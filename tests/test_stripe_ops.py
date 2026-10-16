import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import stripe

from billing_e2e.stripe_ops import StripeRun


class FakeClocks:
    """In-memory stand-in for client.v1.test_helpers.test_clocks."""

    def __init__(self, statuses_after_advance=("advancing", "ready")):
        self.clocks = {}
        self.statuses_after_advance = statuses_after_advance
        self.pending = []

    def create(self, params):
        clock = SimpleNamespace(id=f"clock_{len(self.clocks)}", status="ready", **params)
        self.clocks[clock.id] = clock
        return clock

    def advance(self, clock_id, params):
        self.clocks[clock_id].frozen_time = params["frozen_time"]
        self.pending = list(self.statuses_after_advance)

    def retrieve(self, clock_id):
        clock = self.clocks[clock_id]
        if self.pending:
            clock.status = self.pending.pop(0)
        return SimpleNamespace(**vars(clock))

    def delete(self, clock_id):
        del self.clocks[clock_id]


class Recorder:
    """Records the calls made to one Stripe service and answers with canned values."""

    def __init__(self, **answers):
        self.calls = []
        self.answers = answers

    def __getattr__(self, method):
        def call(*args):
            self.calls.append((method, *args))
            answer = self.answers.get(method)
            return answer(*args) if callable(answer) else answer

        return call


def page(*items):
    return SimpleNamespace(data=list(items), auto_paging_iter=lambda: iter(items))


def event(event_id, event_type, created, **obj):
    data = SimpleNamespace(object=SimpleNamespace(**obj))
    return SimpleNamespace(id=event_id, type=event_type, created=created, data=data)


def fake_client(clocks=None, **services) -> Any:
    v1 = SimpleNamespace(
        test_helpers=SimpleNamespace(test_clocks=clocks or FakeClocks()),
        customers=Recorder(create=lambda params: SimpleNamespace(id="cus_1", **params)),
        payment_methods=Recorder(attach=lambda pm, _params: SimpleNamespace(id=f"{pm}_attached")),
        subscriptions=Recorder(create=lambda params: SimpleNamespace(id="sub_1", status="active", **params)),
        prices=Recorder(list=page()),
        invoices=Recorder(list=page()),
        events=Recorder(list=page()),
    )
    vars(v1).update(services)
    return SimpleNamespace(v1=v1)


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("billing_e2e.checks.time.sleep", lambda _s: None)


def test_a_run_creates_a_tagged_customer_on_its_own_clock_and_deletes_the_clock():
    clocks = FakeClocks()
    with StripeRun(fake_client(clocks), "renewal") as run:
        assert run.customer.test_clock == run.clock.id
        assert run.customer.metadata == {"billing_e2e_run": run.tag}
        assert run.clock.name == f"billing-e2e renewal {run.tag}"
    assert clocks.clocks == {}


def test_the_clock_is_deleted_even_when_the_scenario_fails():
    clocks = FakeClocks()
    with pytest.raises(AssertionError), StripeRun(fake_client(clocks), "renewal"):
        raise AssertionError("a check failed")
    assert clocks.clocks == {}


def test_advance_waits_until_the_clock_is_ready():
    clocks = FakeClocks(statuses_after_advance=("advancing", "advancing", "ready"))
    with StripeRun(fake_client(clocks), "renewal") as run:
        target = run.now + timedelta(days=31)
        run.advance(target)
        assert run.now == target.replace(microsecond=0)
        assert run.clock.status == "ready"


def test_advance_refuses_to_go_back_in_time():
    with StripeRun(fake_client(FakeClocks()), "renewal") as run, pytest.raises(ValueError, match="back in time"):
        run.advance(run.now - timedelta(days=1))


def test_advance_reports_a_stripe_failure():
    clocks = FakeClocks(statuses_after_advance=("advancing", "internal_failure"))
    with StripeRun(fake_client(clocks), "renewal") as run, pytest.raises(RuntimeError, match="internal_failure"):
        run.advance(run.now + timedelta(days=1))


def test_attach_card_makes_the_test_card_the_default_for_invoices():
    client = fake_client()
    with StripeRun(client, "renewal") as run:
        run.attach_card("fails_later")
    assert ("attach", "pm_card_chargeCustomerFail", {"customer": "cus_1"}) in client.v1.payment_methods.calls
    update = {"invoice_settings": {"default_payment_method": "pm_card_chargeCustomerFail_attached"}}
    assert ("update", "cus_1", update) in client.v1.customers.calls


def test_attach_card_refuses_an_unknown_card():
    with StripeRun(fake_client(), "renewal") as run, pytest.raises(ValueError, match="ok, declined, fails_later, 3ds"):
        run.attach_card("visa")


def test_subscribe_tags_the_subscription_and_keeps_the_caller_params():
    client = fake_client()
    with StripeRun(client, "renewal") as run:
        run.subscribe("price_123", {"trial_period_days": 7, "metadata": {"user_id": "42"}})
        assert run.subscription_id == "sub_1"
    (_, params), *_ = client.v1.subscriptions.calls
    assert params["customer"] == "cus_1"
    assert params["items"] == [{"price": "price_123"}]
    assert params["trial_period_days"] == 7
    assert params["metadata"] == {"user_id": "42", "billing_e2e_run": run.tag}


def test_a_lookup_key_is_resolved_to_its_price():
    client = fake_client(prices=Recorder(list=page(SimpleNamespace(id="price_found"))))
    with StripeRun(client, "renewal") as run:
        assert run.resolve_price("business_monthly") == "price_found"
    assert client.v1.prices.calls == [("list", {"lookup_keys": ["business_monthly"], "active": True, "limit": 1})]


def test_an_unknown_lookup_key_is_a_clear_error():
    with StripeRun(fake_client(), "renewal") as run, pytest.raises(ValueError, match="No active price"):
        run.resolve_price("nope")


def test_settle_moves_the_clock_just_past_the_last_planned_finalization():
    clocks = FakeClocks()
    with StripeRun(fake_client(clocks), "renewal") as run:
        start = run.clock.frozen_time
        drafts = page(
            SimpleNamespace(automatically_finalizes_at=start + 3600),
            SimpleNamespace(automatically_finalizes_at=start + 7200),
            SimpleNamespace(automatically_finalizes_at=None),
        )
        run.client.v1.invoices.answers["list"] = drafts
        run.settle()
        assert run.clock.frozen_time == start + 7200 + 60


def test_settle_does_nothing_without_drafts():
    with StripeRun(fake_client(), "renewal") as run:
        start = run.clock.frozen_time
        run.settle()
        assert run.clock.frozen_time == start


def test_events_keeps_only_the_run_customer_oldest_first():
    events = Recorder(
        list=page(
            event("evt_3", "invoice.paid", 30, id="in_1", customer="cus_1"),
            event("evt_x", "invoice.paid", 20, id="in_9", customer="cus_other"),
            event("evt_1", "customer.created", 10, id="cus_1"),
            event("evt_2", "test_helpers.test_clock.ready", 15),  # no id, no customer
        )
    )
    with StripeRun(fake_client(events=events), "renewal") as run:
        assert [e.id for e in run.events()] == ["evt_1", "evt_3"]
        run.events("invoice.paid")
    assert events.calls[-1][1]["type"] == "invoice.paid"
    assert events.calls[-1][1]["created"] == {"gte": run.started_at}


def test_wait_for_event_never_returns_the_same_event_twice():
    first = event("evt_1", "invoice.paid", 10, customer="cus_1")
    second = event("evt_2", "invoice.paid", 20, customer="cus_1")
    events = Recorder(list=page(first))
    with StripeRun(fake_client(events=events), "renewal") as run:
        assert run.wait_for_event("invoice.paid").id == "evt_1"
        events.answers["list"] = page(first, second)
        assert run.wait_for_event("invoice.paid").id == "evt_2"
        with pytest.raises(TimeoutError, match=r"invoice\.paid"):
            run.wait_for_event("invoice.paid", timeout=0)


needs_stripe = pytest.mark.skipif(
    not os.environ.get("BILLING_E2E_STRIPE_KEY"), reason="needs BILLING_E2E_STRIPE_KEY (a test key)"
)


def real_client(monkeypatch):
    from billing_e2e.safety import check_key, confirm_test_mode

    monkeypatch.undo()  # real waiting against the real API
    key = os.environ["BILLING_E2E_STRIPE_KEY"]
    check_key(key)
    client = stripe.StripeClient(key)
    confirm_test_mode(client)
    return client


@needs_stripe
def test_against_stripe(monkeypatch):
    client = real_client(monkeypatch)
    with StripeRun(client, "smoke") as run:
        clock_id, start = run.clock.id, run.now
        assert client.v1.customers.retrieve(run.customer.id).test_clock == clock_id
        run.advance(start + timedelta(days=1))
        assert run.now == start + timedelta(days=1)

    with pytest.raises(stripe.InvalidRequestError) as gone:
        client.v1.test_helpers.test_clocks.retrieve(clock_id)
    assert gone.value.code == "resource_missing"


@needs_stripe
@pytest.mark.skipif(not os.environ.get("BILLING_E2E_PRICE"), reason="needs BILLING_E2E_PRICE (a monthly test price)")
def test_a_failed_renewal_against_stripe(monkeypatch):
    client = real_client(monkeypatch)
    with StripeRun(client, "failed-renewal") as run:
        run.attach_card("ok")
        assert run.subscribe(os.environ["BILLING_E2E_PRICE"]).status == "active"
        run.attach_card("fails_later")
        period_end = run.subscription["items"].data[0].current_period_end
        run.advance(datetime.fromtimestamp(period_end, tz=timezone.utc))
        run.settle()
        assert run.subscription.status == "past_due"
        assert run.wait_for_event("invoice.payment_failed").data.object.billing_reason == "subscription_cycle"
