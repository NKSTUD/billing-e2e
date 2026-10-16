"""Everything that talks to Stripe: test clock, customer, subscription, invoices, test cards, events, cleanup."""

import time
import uuid
from datetime import datetime, timezone

import stripe

from billing_e2e.checks import wait_until

# Stripe usually finishes advancing a clock in seconds; a clock with many objects can take a few minutes
ADVANCE_TIMEOUT = 300
EVENT_TIMEOUT = 60

# Stripe's ready-made test payment methods: nothing to type, no card number in the code
CARDS = {
    "ok": "pm_card_visa",
    "declined": "pm_card_chargeDeclined",  # refused as soon as it is attached
    # Attaches fine, then every charge fails, the first one included: subscribe with "ok", then switch to this card
    # to make the renewal fail (the subscription goes past_due and Stripe schedules its retries)
    "fails_later": "pm_card_chargeCustomerFail",
    "3ds": "pm_card_threeDSecure2Required",  # every payment needs authentication: the subscription stays incomplete
}


class StripeRun:
    """One isolated run: its own test clock, and a customer attached to it.

    Deleting the clock deletes the customer and everything attached to it, so `cleanup` (always called when used as a
    context manager, even after a failure) leaves the account as it was. Existing objects are never touched.
    """

    def __init__(self, client: stripe.StripeClient, name: str):
        self.client = client
        self.name = name
        self.tag = uuid.uuid4().hex[:8]  # marks every object of this run
        self.clock: stripe.test_helpers.TestClock | None = None
        self.customer: stripe.Customer | None = None
        self.subscription_id: str | None = None
        self.started_at = 0  # real time: events are dated with it, not with the clock
        self.seen_events: set[str] = set()

    def __enter__(self) -> "StripeRun":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    def start(self) -> None:
        self.started_at = int(time.time()) - 5  # margin for a clock slightly ahead of Stripe's
        self.clock = clock = self.client.v1.test_helpers.test_clocks.create(
            {"frozen_time": int(time.time()), "name": f"billing-e2e {self.name} {self.tag}"}
        )
        self.customer = self.client.v1.customers.create(
            {
                "test_clock": clock.id,
                "email": f"billing-e2e+{self.tag}@example.com",
                "name": f"billing-e2e {self.name}",
                "metadata": {"billing_e2e_run": self.tag},
            }
        )

    @property
    def now(self) -> datetime:
        """The simulated time of this run."""
        return datetime.fromtimestamp(self._clock().frozen_time, tz=timezone.utc)

    def advance(self, to: datetime) -> None:
        """Move simulated time forward to `to`, and return once Stripe has processed everything due until then
        (renewals, invoices, payment attempts)."""
        clock = self._clock()
        target = int(to.timestamp())
        if target <= clock.frozen_time:
            raise ValueError(f"Cannot go back in time: the clock is already at {self.now:%Y-%m-%d %H:%M} UTC")
        self.client.v1.test_helpers.test_clocks.advance(clock.id, {"frozen_time": target})

        def ready() -> bool:
            self.clock = current = self.client.v1.test_helpers.test_clocks.retrieve(clock.id)
            if current.status == "internal_failure":
                raise RuntimeError("Stripe failed to advance the test clock (status internal_failure)")
            return current.status == "ready"

        wait_until(ready, what="the test clock to finish advancing", timeout=ADVANCE_TIMEOUT)

    def settle(self) -> None:
        """Let Stripe finalize and charge the draft invoices, as it does in production.

        A renewal invoice stays a draft for about an hour before Stripe finalizes it and attempts the payment. Rather
        than forcing that through the API (which skips what Stripe really does, retries included), move the clock just
        past the moment Stripe planned to finalize the drafts.
        """
        drafts = self.client.v1.invoices.list({"customer": self._customer().id, "status": "draft", "limit": 100})
        due = [inv.automatically_finalizes_at for inv in drafts.data if inv.automatically_finalizes_at]
        if due:
            self.advance(datetime.fromtimestamp(max(due) + 60, tz=timezone.utc))

    def attach_card(self, card: str) -> None:
        """Give the customer one of Stripe's test cards and make it the default for every invoice."""
        if card not in CARDS:
            raise ValueError(f"Unknown test card {card!r}: use one of {', '.join(CARDS)}")
        customer = self._customer()
        method = self.client.v1.payment_methods.attach(CARDS[card], {"customer": customer.id})
        self.client.v1.customers.update(customer.id, {"invoice_settings": {"default_payment_method": method.id}})

    def subscribe(self, price: str, params: dict | None = None) -> stripe.Subscription:
        """Subscribe the customer to `price` (a price id, or a lookup key). `params` adds any Stripe subscription
        parameter (trial, coupon, metadata your application expects…). A payment that fails or needs authentication
        leaves the subscription `incomplete`, as on a real checkout, so a scenario can check it."""
        params = dict(params or {})
        subscription = self.client.v1.subscriptions.create(
            {
                **params,
                "customer": self._customer().id,
                "items": [{"price": self.resolve_price(price)}],
                "metadata": {**params.get("metadata", {}), "billing_e2e_run": self.tag},
            }
        )
        self.subscription_id = subscription.id
        return subscription

    def resolve_price(self, price: str) -> str:
        if price.startswith("price_"):
            return price
        found = self.client.v1.prices.list({"lookup_keys": [price], "active": True, "limit": 1}).data
        if not found:
            raise ValueError(f"No active price with the lookup key {price!r} (or pass a price id, price_…)")
        return found[0].id

    @property
    def subscription(self) -> stripe.Subscription:
        """The current state of the run's subscription, fetched from Stripe."""
        if self.subscription_id is None:
            raise RuntimeError("No subscription yet: subscribe first")
        return self.client.v1.subscriptions.retrieve(self.subscription_id)

    def events(self, event_type: str | None = None) -> list[stripe.Event]:
        """Events about this run's customer since the run started, oldest first.

        Stripe cannot filter events by customer, so this reads the account's recent events and keeps those whose
        object belongs to the run's customer (the customer itself, its subscriptions, invoices, payments…).
        """
        customer_id = self._customer().id
        params: dict = {"created": {"gte": self.started_at}, "limit": 100}
        if event_type:
            params["type"] = event_type
        mine = []
        for event in self.client.v1.events.list(params).auto_paging_iter():
            obj = event.data.object
            if customer_id in (getattr(obj, "customer", None), getattr(obj, "id", None)):
                mine.append(event)
        return sorted(mine, key=lambda e: e.created)

    def wait_for_event(self, event_type: str, timeout: float = EVENT_TIMEOUT) -> stripe.Event:
        """Wait for the next `event_type` event of this run not returned before, and return it."""

        def new_event() -> stripe.Event | None:
            return next((e for e in self.events(event_type) if e.id not in self.seen_events), None)

        event = wait_until(new_event, what=f"a {event_type} event", timeout=timeout, interval=2)
        self.seen_events.add(event.id)
        return event

    def cleanup(self) -> None:
        if self.clock is None:
            return
        try:
            self.client.v1.test_helpers.test_clocks.delete(self.clock.id)
        except stripe.InvalidRequestError as exc:
            if exc.code != "resource_missing":  # already gone is fine
                raise
        self.clock = self.customer = None
        self.subscription_id = None

    def _clock(self) -> stripe.test_helpers.TestClock:
        if self.clock is None:
            raise RuntimeError("The run has not started (or was cleaned up)")
        return self.clock

    def _customer(self) -> stripe.Customer:
        if self.customer is None:
            raise RuntimeError("The run has not started (or was cleaned up)")
        return self.customer
