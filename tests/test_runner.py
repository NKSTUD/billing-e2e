import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest
import stripe

from billing_e2e import runner as runner_module
from billing_e2e.runner import Runner, describe
from billing_e2e.scenario import (
    Advance,
    Cancel,
    Card,
    ChangePrice,
    CommandCheck,
    Config,
    CustomerIs,
    Duration,
    ExpectEvent,
    HttpCheck,
    InvoiceIs,
    Scenario,
    Subscribe,
    SubscriptionIs,
)

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


class FakeRun:
    """Stands in for StripeRun: records what the runner asks Stripe to do."""

    instances: ClassVar[list["FakeRun"]] = []

    def __init__(self, client, name):
        self.name, self.tag, self.calls = name, "a1b2", []
        self.customer = SimpleNamespace(id="cus_1", email="billing-e2e+a1b2@example.com")
        self.subscription_id = None
        self.started, self.now, self.period_end = True, NOW, datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.status, self.cleaned = "active", False
        FakeRun.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.cleaned = True

    def attach_card(self, card):
        self.calls.append(("card", card))
        if card == "declined":
            raise stripe.CardError("Your card was declined.", None, "card_declined")

    def subscribe(self, price, params):
        self.calls.append(("subscribe", price, params))
        self.subscription_id = "sub_1"
        return SimpleNamespace(id="sub_1", status=self.status)

    def advance(self, to):
        self.calls.append(("advance", to))
        self.now = to

    def settle(self):
        self.calls.append(("settle",))

    def wait_for_event(self, event_type):
        self.calls.append(("event", event_type))
        if event_type == "never.happens":
            raise TimeoutError("Gave up waiting for a never.happens event after 60 s")
        return SimpleNamespace(id="evt_1")

    @property
    def subscription(self):
        return stripe.Subscription.construct_from(
            {"id": "sub_1", "status": self.status, "metadata": {"run": "a1b2"}}, "sk_test_x"
        )

    def change_price(self, price, params):
        self.calls.append(("change_price", price, params))
        items = {"object": "list", "data": [{"price": {"id": price}}]}
        return stripe.Subscription.construct_from({"id": "sub_1", "items": items}, "sk_test_x")

    @property
    def latest_invoice(self):
        return stripe.Invoice.construct_from({"id": "in_1", "total": -7021, "status": "paid"}, "sk_test_x")

    def fetch_customer(self):
        return stripe.Customer.construct_from({"id": "cus_1", "balance": -7021}, "sk_test_x")

    def cancel(self, when):
        self.calls.append(("cancel", when))
        return SimpleNamespace(id="sub_1", status="active", cancel_at_period_end=True)


@pytest.fixture(autouse=True)
def fake_stripe(monkeypatch):
    FakeRun.instances = []
    monkeypatch.setattr(runner_module, "StripeRun", FakeRun)
    monkeypatch.setattr(runner_module, "STRIPE_CHECK_TIMEOUT", 0)
    monkeypatch.setattr("billing_e2e.checks.CHECK_TIMEOUT", 0)
    monkeypatch.setattr("billing_e2e.checks.time.sleep", lambda _s: None)


def play(*steps, config: Config | None = None, http: Any = None):
    runner = Runner(config or Config(stripe_key="sk_test_x"), client=SimpleNamespace(), http=http)  # type: ignore[arg-type]
    seen = []
    result = runner.run(Scenario("demo", list(steps)), on_step=seen.append)
    return result, seen, FakeRun.instances[0]


def python(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_every_step_drives_stripe_and_the_run_passes():
    result, seen, run = play(
        Card("ok"),
        Subscribe("business_monthly", {"metadata": {"email": "billing-e2e+{run}@example.com"}}),
        ExpectEvent("invoice.paid"),
        SubscriptionIs({"status": "active", "metadata.run": "{run}"}),
        Advance("period_end"),
        Advance(Duration(3, "day")),
        ChangePrice("premium_monthly", {"proration_behavior": "always_invoice"}),
        Cancel("period_end"),
    )
    assert result.passed, [s.detail for s in seen]
    assert run.calls == [
        ("card", "ok"),
        ("subscribe", "business_monthly", {"metadata": {"email": "billing-e2e+a1b2@example.com"}}),
        ("event", "invoice.paid"),
        ("advance", datetime(2026, 11, 16, tzinfo=timezone.utc)),
        ("settle",),
        ("advance", datetime(2026, 11, 19, tzinfo=timezone.utc)),
        ("settle",),
        ("change_price", "premium_monthly", {"proration_behavior": "always_invoice"}),
        ("cancel", "period_end"),
    ]
    assert seen[-1].detail == "sub_1 is active, ends at period end"
    assert seen[-1].simulated == datetime(2026, 11, 19, tzinfo=timezone.utc)
    assert run.cleaned


def test_a_declined_card_passes_when_stripe_refuses_it():
    result, seen, _ = play(Card("declined"))
    assert result.passed
    assert "refused by Stripe" in seen[0].detail


def test_the_first_failure_stops_the_scenario_and_counts_what_did_not_run():
    result, seen, run = play(Card("ok"), ExpectEvent("never.happens"), Cancel("now"), Cancel("now"))
    assert not result.passed
    assert [s.passed for s in seen] == [True, False]
    assert seen[1].detail == "Gave up waiting for a never.happens event after 60 s"
    assert result.not_run == 2
    assert ("cancel", "now") not in run.calls
    assert run.cleaned


def test_a_wrong_subscription_state_says_what_stripe_has():
    result, seen, _ = play(SubscriptionIs({"status": "past_due"}))
    assert not result.passed
    assert "status is 'active', expected 'past_due'" in seen[0].detail


def test_commands_get_the_run_in_their_environment_and_variables_filled():
    command = python(
        "import os; print(os.environ['BILLING_E2E_CUSTOMER'], os.environ['BILLING_E2E_RUN'], '{customer}')"
    )
    result, seen, _ = play(CommandCheck(command))
    assert result.passed
    assert seen[0].detail == "cus_1 a1b2 cus_1"


def test_http_checks_call_the_app_with_the_configured_headers():
    seen_requests = []

    def app(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"plan": {"name": "Business"}, "run": "a1b2"})

    http = httpx.Client(transport=httpx.MockTransport(app), base_url="http://app.test")
    config = Config(stripe_key="sk_test_x", base_url="http://app.test", headers={"Authorization": "Bearer t"})
    result, _, _ = play(
        HttpCheck("GET", "/api/{customer}/account", {"plan.name": "Business", "run": "{run}"}), config=config, http=http
    )
    assert result.passed
    assert seen_requests[0].url.path == "/api/cus_1/account"
    assert seen_requests[0].headers["Authorization"] == "Bearer t"


def test_an_http_check_without_base_url_is_a_clear_failure():
    result, seen, _ = play(HttpCheck("GET", "/api/account", {"plan": "x"}))
    assert not result.passed
    assert "needs app.base_url" in seen[0].detail


def test_a_failing_setup_runs_no_step_but_still_tears_down(tmp_path):
    marker = tmp_path / "teardown-ran"
    config = Config(
        stripe_key="sk_test_x",
        setup=python("import sys; print('no test user'); sys.exit(1)"),
        teardown=python(f"open(r'{marker}', 'w').write(__import__('os').environ['BILLING_E2E_RUN'])"),
    )
    result, seen, run = play(Card("ok"), config=config)
    assert not result.passed
    assert seen == []
    assert result.not_run == 1
    assert result.errors == [f"setup failed: `{config.setup}` exited with 1: no test user"]
    assert marker.read_text() == "a1b2"
    assert run.cleaned


def test_a_failing_teardown_is_reported():
    config = Config(stripe_key="sk_test_x", teardown=python("import sys; sys.exit(2)"))
    result, _, _ = play(Card("ok"), config=config)
    assert not result.passed
    assert result.errors[0].startswith("teardown failed")


@pytest.mark.parametrize(
    ("step", "label"),
    [
        (Advance(Duration(1, "month")), "advance 1 month"),
        (Advance(Duration(3, "day")), "advance 3 days"),
        (Advance("period_end"), "advance period_end"),
        (HttpCheck("GET", "/api/account", {"a": 1}), "check GET /api/account"),
        (Cancel("period_end"), "cancel period end"),
        (SubscriptionIs({"status": "active"}), "subscription status = active"),
    ],
)
def test_describe(step, label):
    assert describe(step) == label


@pytest.mark.skipif(
    not (os.environ.get("BILLING_E2E_STRIPE_KEY") and os.environ.get("BILLING_E2E_PRICE")),
    reason="needs BILLING_E2E_STRIPE_KEY and BILLING_E2E_PRICE",
)
def test_a_scenario_against_stripe(monkeypatch):
    monkeypatch.undo()
    client = stripe.StripeClient(os.environ["BILLING_E2E_STRIPE_KEY"])
    scenario = Scenario(
        "runner smoke",
        [
            Card("ok"),
            Subscribe(os.environ["BILLING_E2E_PRICE"], {"metadata": {"run": "{run}"}}),
            ExpectEvent("invoice.paid"),
            Card("fails_later"),
            Advance("period_end"),
            ExpectEvent("invoice.payment_failed"),
            SubscriptionIs({"status": "past_due"}),
            Cancel("now"),
            ExpectEvent("customer.subscription.deleted"),
        ],
    )
    result = Runner(Config(stripe_key="unused"), client).run(scenario)
    assert result.passed, [(s.label, s.detail) for s in result.steps]


def test_project_vars_fill_the_prices():
    config = Config(stripe_key="sk_test_x", vars={"monthly_price": "price_m", "upgrade_price": "price_u"})
    result, _, run = play(Subscribe("{monthly_price}"), ChangePrice("{upgrade_price}"), config=config)
    assert result.passed
    assert run.calls == [("subscribe", "price_m", {}), ("change_price", "price_u", {})]


def test_after_subscribe_runs_once_the_subscription_exists():
    hook = python("import os, sys; sys.exit(os.environ['BILLING_E2E_SUBSCRIPTION'] != 'sub_1')")
    config = Config(stripe_key="sk_test_x", after_subscribe=hook)
    result, _, _ = play(Subscribe("price_m"), config=config)
    assert result.passed


def test_a_failing_after_subscribe_fails_the_step():
    config = Config(stripe_key="sk_test_x", after_subscribe=python("import sys; print('no such plan'); sys.exit(1)"))
    result, seen, _ = play(Subscribe("price_m"), config=config)
    assert not result.passed
    assert seen[0].detail.startswith("after_subscribe failed:")
    assert seen[0].detail.endswith("exited with 1: no such plan")


def test_invoice_and_customer_steps_check_amounts_and_credit():
    _, seen, _ = play(
        InvoiceIs({"total": {"lt": 0}, "status": "paid"}),
        CustomerIs({"balance": {"lt": 0}}),
        CustomerIs({"balance": {"gte": 0}}),
    )
    assert [s.passed for s in seen] == [True, True, False]
    assert seen[0].label == "latest invoice total lt 0, status = paid"
    assert seen[0].detail == "in_1 matches"
    assert "balance is -7021, expected gte 0" in seen[2].detail


def test_secrets_in_a_step_never_reach_its_label_or_detail():
    result, seen, _ = play(CommandCheck(python("import sys; sys.exit(1)") + " sk_live_51Secret"))
    assert not result.passed
    assert "51Secret" not in seen[0].label + seen[0].detail
