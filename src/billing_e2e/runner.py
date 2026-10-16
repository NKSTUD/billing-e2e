"""Plays a scenario step by step and collects the result of each step."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import stripe

from billing_e2e.checks import CheckFailed, Result, command_check, eventually, http_check, lookup
from billing_e2e.safety import redact
from billing_e2e.scenario import (
    Advance,
    Cancel,
    Card,
    ChangePrice,
    CommandCheck,
    Config,
    ExpectEvent,
    HttpCheck,
    Scenario,
    Step,
    Subscribe,
    SubscriptionIs,
    fill,
)
from billing_e2e.stripe_ops import StripeRun

# Stripe's own state is up to date as soon as a call returns, but a status can take a moment to settle
STRIPE_CHECK_TIMEOUT = 10


@dataclass
class StepResult:
    label: str
    passed: bool
    detail: str
    seconds: float
    simulated: datetime | None = None  # the test clock's time once the step is done


@dataclass
class RunResult:
    scenario: str
    steps: list[StepResult] = field(default_factory=list)
    not_run: int = 0  # steps left after a failure
    errors: list[str] = field(default_factory=list)  # setup, teardown, anything outside a step

    @property
    def passed(self) -> bool:
        return not self.errors and not self.not_run and all(step.passed for step in self.steps)


def describe(step: Step) -> str:
    """A short label for reports: "advance 1 month", "check GET /api/account"."""
    match step:
        case Card(card):
            return f"card {card}"
        case Subscribe(price):
            return f"subscribe {price}"
        case Advance(to):
            return f"advance {to}" if isinstance(to, str) else f"advance {to.amount} {to.unit}{'s' * (to.amount > 1)}"
        case ExpectEvent(event_type):
            return f"expect event {event_type}"
        case SubscriptionIs(expect):
            return "subscription " + ", ".join(f"{k} = {v}" for k, v in expect.items())
        case HttpCheck(method, path):
            return f"check {method} {path}"
        case CommandCheck(command):
            return f"check `{command}`"
        case ChangePrice(price):
            return f"change price to {price}"
        case Cancel(when):
            return f"cancel {when.replace('_', ' ')}"
    raise TypeError(step)


class Runner:
    def __init__(self, config: Config, client: stripe.StripeClient, http: httpx.Client | None = None):
        self.config = config
        self.client = client
        self.http = http or httpx.Client(base_url=config.base_url or "", timeout=30)

    def run(self, scenario: Scenario, on_step: Callable[[StepResult], None] | None = None) -> RunResult:
        result = RunResult(scenario.name)
        with StripeRun(self.client, scenario.name) as run:
            try:
                if self.config.setup:
                    self._hook("setup", self.config.setup, run)
                for number, step in enumerate(scenario.steps):
                    outcome = self._step(step, run)
                    result.steps.append(outcome)
                    if on_step:
                        on_step(outcome)
                    if not outcome.passed:
                        result.not_run = len(scenario.steps) - number - 1
                        break
            except CheckFailed as exc:  # setup failed: no step runs
                result.not_run = len(scenario.steps)
                result.errors.append(str(exc))
            finally:
                if self.config.teardown:
                    try:
                        self._hook("teardown", self.config.teardown, run)
                    except CheckFailed as exc:
                        result.errors.append(str(exc))
        return result

    def _step(self, step: Step, run: StripeRun) -> StepResult:
        started = time.monotonic()
        try:
            detail = self._play(step, run)
            passed = True
        except (CheckFailed, TimeoutError, ValueError, RuntimeError, stripe.StripeError) as exc:
            passed, detail = False, redact(str(exc))
        simulated = run.now if run.started else None
        return StepResult(describe(step), passed, detail, time.monotonic() - started, simulated)

    def _play(self, step: Step, run: StripeRun) -> str:
        """Do the step; return what happened, or raise with why it failed."""
        variables = self._variables(run)
        match step:
            case Card("declined"):
                try:
                    run.attach_card("declined")
                except stripe.CardError as exc:
                    return f"refused by Stripe, as a real declined card ({exc.code})"
                raise CheckFailed("Stripe accepted the declined test card")
            case Card(card):
                run.attach_card(card)
                return "set as the default payment method"
            case Subscribe(price, params):
                subscription = run.subscribe(fill(price, variables), fill(params, variables))
                if self.config.after_subscribe:
                    self._hook("after_subscribe", self.config.after_subscribe, run)
                return f"{subscription.id} is {subscription.status}"
            case Advance(to):
                target = run.period_end if isinstance(to, str) else to.after(run.now)
                run.advance(target)
                run.settle()
                return f"clock at {run.now:%Y-%m-%d %H:%M} UTC"
            case ExpectEvent(event_type):
                return f"received {run.wait_for_event(event_type).id}"
            case SubscriptionIs(expect):
                return self._check_subscription(run, fill(expect, variables)).detail
            case HttpCheck(method, path, expect):
                if not self.config.base_url:
                    raise CheckFailed("an HTTP check needs app.base_url in the config")
                url, wanted, headers = fill(path, variables), fill(expect, variables), self.config.headers
                return eventually(
                    lambda: http_check(self.http, method, url, wanted, headers), what=f"{method} {url}"
                ).detail
            case CommandCheck(command):
                env, command = self._env(run), fill(command, variables)
                return eventually(lambda: command_check(command, env), what=f"`{command}`").detail
            case ChangePrice(price, params):
                subscription = run.change_price(fill(price, variables), fill(params, variables))
                return f"{subscription.id} is now on {subscription['items'].data[0].price.id}"
            case Cancel(when):
                subscription = run.cancel(when)
                return f"{subscription.id} is {subscription.status}" + (
                    ", ends at period end" if subscription.cancel_at_period_end else ""
                )
        raise TypeError(step)

    def _check_subscription(self, run: StripeRun, expect: dict) -> Result:
        def check() -> Result:
            data = run.subscription.to_dict()
            wrong = [
                f"{path} is {lookup(data, path)!r}, expected {value!r}"
                for path, value in expect.items()
                if lookup(data, path) != value
            ]
            return Result(not wrong, "; ".join(wrong) or "matches")

        return eventually(check, what="the Stripe subscription", timeout=STRIPE_CHECK_TIMEOUT)

    def _hook(self, name: str, command: str, run: StripeRun) -> None:
        outcome = command_check(command, self._env(run))
        if not outcome.passed:
            raise CheckFailed(f"{name} failed: {outcome.detail}")

    def _variables(self, run: StripeRun) -> dict[str, str]:
        run_values = {"run": run.tag, "customer": run.customer.id, "subscription": run.subscription_id or ""}
        return {**self.config.vars, **run_values}

    def _env(self, run: StripeRun) -> dict[str, str]:
        """What a command of the application gets to know which run it is about."""
        return {
            "BILLING_E2E_RUN": run.tag,
            "BILLING_E2E_CUSTOMER": run.customer.id,
            "BILLING_E2E_CUSTOMER_EMAIL": run.customer.email or "",
            "BILLING_E2E_SUBSCRIPTION": run.subscription_id or "",
        }
