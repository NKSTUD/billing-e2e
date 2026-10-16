"""Reads a scenario file (YAML) into a list of steps, with clear errors on a bad file."""

import calendar
import difflib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from billing_e2e.stripe_ops import CARDS


class ScenarioError(ValueError):
    pass


# --- durations -------------------------------------------------------------------------------------------------------

UNITS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}


@dataclass(frozen=True)
class Duration:
    amount: int
    unit: str  # minute, hour, day, week, month, year

    def after(self, moment: datetime) -> datetime:
        """`moment` plus this duration; months and years follow the calendar (Jan 31 + 1 month = Feb 28)."""
        if self.unit in UNITS:
            return moment + timedelta(seconds=self.amount * UNITS[self.unit])
        months = moment.month - 1 + self.amount * (12 if self.unit == "year" else 1)
        year, month = moment.year + months // 12, months % 12 + 1
        return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def parse_duration(text: str) -> Duration:
    match = re.fullmatch(r"(\d+)\s*(minute|hour|day|week|month|year)s?", text.strip())
    if not match or int(match[1]) == 0:
        raise ValueError(f"{text!r} is not a duration: write e.g. 3 days, 1 month, period_end")
    return Duration(int(match[1]), match[2])


# --- steps -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    card: str


@dataclass(frozen=True)
class Subscribe:
    price: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Advance:
    to: Duration | str  # a Duration, or "period_end"


@dataclass(frozen=True)
class ExpectEvent:
    type: str


@dataclass(frozen=True)
class SubscriptionIs:
    expect: dict


@dataclass(frozen=True)
class HttpCheck:
    method: str
    path: str
    expect: dict


@dataclass(frozen=True)
class CommandCheck:
    command: str


@dataclass(frozen=True)
class ChangePrice:
    price: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Cancel:
    when: str  # now, period_end


Step = Card | Subscribe | Advance | ExpectEvent | SubscriptionIs | HttpCheck | CommandCheck | ChangePrice | Cancel


@dataclass(frozen=True)
class Scenario:
    name: str
    steps: list[Step]


@dataclass(frozen=True)
class Config:
    stripe_key: str
    base_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    setup: str | None = None
    teardown: str | None = None


# --- parsing ---------------------------------------------------------------------------------------------------------


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty text")
    return value.strip()


def _mapping(value: Any, what: str) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{what} must be a non-empty mapping, e.g. {{status: active}}")
    return value


def _only(step: dict, allowed: set[str]) -> None:
    if extra := set(step) - allowed:
        raise ValueError(f"unexpected key(s): {', '.join(sorted(extra))}")


def _card(step: dict) -> Card:
    _only(step, {"card"})
    if step["card"] not in CARDS:
        raise ValueError(f"unknown card {step['card']!r}: use one of {', '.join(CARDS)}")
    return Card(step["card"])


def _subscribe(step: dict) -> Subscribe:
    _only(step, {"subscribe", "params"})
    params = _mapping(step["params"], "params") if "params" in step else {}
    return Subscribe(_text(step["subscribe"], "subscribe"), params)


def _advance(step: dict) -> Advance:
    _only(step, {"advance"})
    to = _text(step["advance"], "advance")
    return Advance(to if to == "period_end" else parse_duration(to))


def _expect_event(step: dict) -> ExpectEvent:
    _only(step, {"expect_event"})
    return ExpectEvent(_text(step["expect_event"], "expect_event"))


def _subscription(step: dict) -> SubscriptionIs:
    _only(step, {"subscription"})
    return SubscriptionIs(_mapping(step["subscription"], "subscription"))


def _check(step: dict) -> HttpCheck | CommandCheck:
    _only(step, {"check"})
    check = _mapping(step["check"], "check")
    if "run" in check:
        _only(check, {"run"})
        return CommandCheck(_text(check["run"], "check.run"))
    methods = [m for m in ("get", "post") if m in check]
    if len(methods) != 1:
        raise ValueError("check needs either run: <command>, or get:/post: <path> with expect:")
    _only(check, {methods[0], "expect"})
    return HttpCheck(methods[0].upper(), _text(check[methods[0]], methods[0]), _mapping(check.get("expect"), "expect"))


def _change_price(step: dict) -> ChangePrice:
    _only(step, {"change_price", "params"})
    params = _mapping(step["params"], "params") if "params" in step else {}
    return ChangePrice(_text(step["change_price"], "change_price"), params)


def _cancel(step: dict) -> Cancel:
    _only(step, {"cancel"})
    if step["cancel"] not in ("now", "period_end"):
        raise ValueError("cancel must be now or period_end")
    return Cancel(step["cancel"])


STEPS = {
    "card": _card,
    "subscribe": _subscribe,
    "advance": _advance,
    "expect_event": _expect_event,
    "subscription": _subscription,
    "check": _check,
    "change_price": _change_price,
    "cancel": _cancel,
}


def parse_step(step: Any) -> Step:
    if not isinstance(step, dict) or not step:
        raise ValueError("each step must be a mapping, e.g. - advance: 1 month")
    kind = next((key for key in step if key in STEPS), None)
    if kind is None:
        first = next(iter(step))
        close = difflib.get_close_matches(str(first), STEPS, n=1)
        hint = f" (did you mean {close[0]}?)" if close else f"; steps are: {', '.join(STEPS)}"
        raise ValueError(f"unknown step {first!r}{hint}")
    return STEPS[kind](step)


def parse_scenario(data: Any, source: str = "scenario") -> Scenario:
    if not isinstance(data, dict):
        raise ScenarioError(f"{source}: expected a mapping with name: and steps:")
    if not isinstance(data.get("steps"), list) or not data["steps"]:
        raise ScenarioError(f"{source}: steps: must be a non-empty list")
    steps = []
    for number, raw in enumerate(data["steps"], start=1):
        try:
            steps.append(parse_step(raw))
        except ValueError as exc:
            kind = next(iter(raw), "?") if isinstance(raw, dict) else "?"
            raise ScenarioError(f"{source}, step {number} ({kind}): {exc}") from None
    return Scenario(str(data.get("name") or Path(source).stem), steps)


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    return parse_scenario(_read_yaml(path), path.name)


# --- config and variables --------------------------------------------------------------------------------------------


def expand_env(value: Any, env: dict[str, str], source: str) -> Any:
    """Replace ${VAR} with the environment value, in every text of `value`. A missing variable is an error, so a
    secret is never sent empty."""

    def one(match: re.Match) -> str:
        if match[1] not in env:
            raise ScenarioError(f"{source}: environment variable {match[1]} is not set")
        return env[match[1]]

    if isinstance(value, str):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", one, value)
    if isinstance(value, dict):
        return {key: expand_env(item, env, source) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item, env, source) for item in value]
    return value


def fill(value: Any, variables: dict[str, str]) -> Any:
    """Replace {run}, {customer}, {subscription}… with the values of the current run. Other braces are left alone."""
    if isinstance(value, str):
        for name, replacement in variables.items():
            value = value.replace("{" + name + "}", replacement)
        return value
    if isinstance(value, dict):
        return {key: fill(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item, variables) for item in value]
    return value


def parse_config(data: Any, source: str = "config", env: dict[str, str] | None = None) -> Config:
    if not isinstance(data, dict):
        raise ScenarioError(f"{source}: expected a mapping with stripe: and app:")
    data = expand_env(data, dict(os.environ) if env is None else env, source)
    stripe_part, app = data.get("stripe") or {}, data.get("app") or {}
    if not isinstance(stripe_part, dict) or not isinstance(app, dict):
        raise ScenarioError(f"{source}: stripe: and app: must be mappings")
    if not stripe_part.get("key"):
        raise ScenarioError(f"{source}: stripe.key is required, e.g. key: ${{STRIPE_TEST_KEY}}")
    headers = app.get("headers") or {}
    if not isinstance(headers, dict):
        raise ScenarioError(f"{source}: app.headers must be a mapping")
    return Config(
        stripe_key=str(stripe_part["key"]).strip(),
        base_url=app.get("base_url"),
        headers={str(k): str(v) for k, v in headers.items()},
        setup=app.get("setup"),
        teardown=app.get("teardown"),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    return parse_config(_read_yaml(path), path.name)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScenarioError(f"{path}: file not found") from None
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path.name}: not valid YAML ({exc})") from None
