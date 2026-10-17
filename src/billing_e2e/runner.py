"""Plays a scenario step by step and collects the result of each step."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import stripe

from billing_e2e.checks import CheckFailed, Result, command_check, compare, eventually, http_check, is_comparison
from billing_e2e.safety import redact
from billing_e2e.scenario import (
    Advance,
    Cancel,
    Card,
    ChangePrice,
    CommandCheck,
    Config,
    CustomerIs,
    ExpectEvent,
    HttpCheck,
    InvoiceIs,
    Scenario,
    Step,
    Subscribe,
    SubscriptionIs,
    fill,
)
from billing_e2e.stripe_ops import StripeRun, account_id

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
    kept: dict[str, str] = field(default_factory=dict)  # with keep: ids left for inspection (clock, customer…)

    @property
    def passed(self) -> bool:
        return not self.errors and not self.not_run and all(step.passed for step in self.steps)


def fields(expect: dict) -> str:
    """{status: active, amount_paid: {gt: 0}} as "status = active, amount_paid gt 0"."""
    parts = []
    for path, value in expect.items():
        if is_comparison(value):
            parts += [f"{path} {name} {limit}" for name, limit in value.items()]
        else:
            parts.append(f"{path} = {value}")
    return ", ".join(parts)


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
            return f"subscription {fields(expect)}"
        case InvoiceIs(expect):
            return f"latest invoice {fields(expect)}"
        case CustomerIs(expect):
            return f"customer {fields(expect)}"
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
    def __init__(
        self, config: Config, client: stripe.StripeClient, http: httpx.Client | None = None, keep: bool = False
    ):
        self.config = config
        self.client = client
        self.keep = keep  # leave Stripe objects and the app's test data (no teardown) for inspection
        self.http = http or httpx.Client(base_url=config.base_url or "", timeout=30)

    def run(self, scenario: Scenario, on_step: Callable[[StepResult], None] | None = None) -> RunResult:
        result = RunResult(scenario.name)
        try:
            with StripeRun(self.client, scenario.name, keep=self.keep) as run:
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
                    if self.keep:
                        result.kept = {"clock": run.clock.id, "customer": run.customer.id}
                        if account := account_id(self.client):
                            result.kept["account"] = account
                        if run.subscription_id:
                            result.kept["subscription"] = run.subscription_id
                    elif self.config.teardown:
                        try:
                            self._hook("teardown", self.config.teardown, run)
                        except CheckFailed as exc:
                            result.errors.append(str(exc))
        except (stripe.StripeError, TimeoutError) as exc:  # Stripe failed creating or deleting the clock or customer
            result.not_run = result.not_run or len(scenario.steps) - len(result.steps)
            result.errors.append(redact(f"Stripe: {exc}"))
        return result

    def _step(self, step: Step, run: StripeRun) -> StepResult:
        started = time.monotonic()
        try:
            detail = self._play(step, run)
            passed = True
        except (CheckFailed, TimeoutError, ValueError, RuntimeError, stripe.StripeError) as exc:
            passed, detail = False, redact(str(exc))
        simulated = run.now if run.started else None
        return StepResult(redact(describe(step)), passed, redact(detail), time.monotonic() - started, simulated)

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
                return self._check_stripe(lambda: run.subscription, fill(expect, variables), "the subscription").detail
            case InvoiceIs(expect):
                return self._check_stripe(
                    lambda: run.latest_invoice, fill(expect, variables), "the latest invoice"
                ).detail
            case CustomerIs(expect):
                return self._check_stripe(run.fetch_customer, fill(expect, variables), "the customer").detail
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

    @staticmethod
    def _check_stripe(fetch: Callable[[], stripe.StripeObject], expect: dict, what: str) -> Result:
        def check() -> Result:
            obj = fetch()
            wrong = compare(obj.to_dict(), expect)
            return Result(not wrong, "; ".join(wrong) or f"{getattr(obj, 'id', what)} matches")

        return eventually(check, what=f"{what} on Stripe", timeout=STRIPE_CHECK_TIMEOUT)

    def _hook(self, name: str, command: str, run: StripeRun) -> None:
        outcome = command_check(command, self._env(run))
        if not outcome.passed:
            raise CheckFailed(redact(f"{name} failed: {outcome.detail}"))

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
