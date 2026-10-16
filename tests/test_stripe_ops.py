import os
from datetime import timedelta
from types import SimpleNamespace

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


def fake_client(clocks):
    customers = SimpleNamespace(create=lambda params: SimpleNamespace(id="cus_1", **params))
    return SimpleNamespace(v1=SimpleNamespace(test_helpers=SimpleNamespace(test_clocks=clocks), customers=customers))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("billing_e2e.checks.time.sleep", lambda _s: None)


def test_a_run_creates_a_tagged_customer_on_its_own_clock_and_deletes_the_clock():
    clocks = FakeClocks()
    with StripeRun(fake_client(clocks), "renewal") as run:
        assert run.customer.test_clock == run.clock.id
        assert run.customer.metadata == {"billing_e2e_run": run.tag}
        assert run.tag in run.clock.name
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


@pytest.mark.skipif(not os.environ.get("BILLING_E2E_STRIPE_KEY"), reason="needs BILLING_E2E_STRIPE_KEY (a test key)")
def test_against_stripe(monkeypatch):
    from billing_e2e.safety import check_key, confirm_test_mode

    monkeypatch.undo()  # real waiting against the real API
    key = os.environ["BILLING_E2E_STRIPE_KEY"]
    check_key(key)
    client = stripe.StripeClient(key)
    confirm_test_mode(client)

    with StripeRun(client, "smoke") as run:
        clock_id, start = run.clock.id, run.now
        assert client.v1.customers.retrieve(run.customer.id).test_clock == clock_id
        run.advance(start + timedelta(days=1))
        assert run.now == start + timedelta(days=1)

    with pytest.raises(stripe.InvalidRequestError) as gone:
        client.v1.test_helpers.test_clocks.retrieve(clock_id)
    assert gone.value.code == "resource_missing"
