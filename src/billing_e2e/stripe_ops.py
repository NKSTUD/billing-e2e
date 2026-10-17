"""Everything that talks to Stripe: test clock, customer, subscription, invoices, test cards, events, cleanup."""

import time
import uuid
from datetime import datetime, timezone
from typing import cast

import stripe
from stripe.params import EventListParams, SubscriptionCreateParams, SubscriptionUpdateParams

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


CLOCK_PREFIX = "billing-e2e "  # every clock this tool creates is named "billing-e2e <scenario> <tag>"


def delete_clock(client: stripe.StripeClient, clock_id: str) -> None:
    """Delete a test clock and everything attached to it. Waits if Stripe is still advancing it (it refuses to delete
    a moving clock, e.g. after Ctrl+C mid-advance); a clock already gone is fine."""

    def deleted() -> bool:
        try:
            client.v1.test_helpers.test_clocks.delete(clock_id)
        except stripe.InvalidRequestError as exc:
            if exc.code != "resource_missing":
                raise
        except stripe.RateLimitError as exc:
            if "advancement underway" not in str(exc):
                raise
            return False
        return True

    wait_until(deleted, what="the test clock to be deleted", timeout=ADVANCE_TIMEOUT, interval=2)


def delete_all_clocks(client: stripe.StripeClient) -> list[str]:
    """Delete every clock billing-e2e created (kept with --keep, or left by a killed run). Returns their names."""
    mine = [
        clock
        for clock in client.v1.test_helpers.test_clocks.list({"limit": 100}).auto_paging_iter()
        if (clock.name or "").startswith(CLOCK_PREFIX)
    ]
    for clock in mine:
        delete_clock(client, clock.id)
    return [clock.name or clock.id for clock in mine]


def account_id(client: stripe.StripeClient) -> str | None:
    """The account the key belongs to, for Dashboard links (without it, a sandbox link opens the default account).
    None when the key may not read it, e.g. a restricted key."""
    try:
        return client.v1.accounts.retrieve_current().id
    except stripe.StripeError:
        return None


class StripeRun:
    """One isolated run: its own test clock, and a customer attached to it.

    Deleting the clock deletes the customer and everything attached to it, so `cleanup` (always called when used as a
    context manager, even after a failure) leaves the account as it was. Existing objects are never touched.
    """

    def __init__(self, client: stripe.StripeClient, name: str, keep: bool = False):
        self.client = client
        self.name = name
        self.keep = keep  # leave the clock and its objects for inspection (delete later with `billing-e2e clean`)
        self.tag = uuid.uuid4().hex[:8]  # marks every object of this run
        self._clock: stripe.test_helpers.TestClock | None = None
        self._customer: stripe.Customer | None = None
        self.subscription_id: str | None = None
        self.started_at = 0  # real time: events are dated with it, not with the clock
        self.seen_events: set[str] = set()

    def __enter__(self) -> "StripeRun":
        try:
            self.start()
        except BaseException:
            # The clock may exist even if the customer could not be created: __exit__ never runs when __enter__ fails
            self.cleanup()
            raise
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    def start(self) -> None:
        self.started_at = int(time.time()) - 5  # margin for a clock slightly ahead of Stripe's
        self._clock = clock = self.client.v1.test_helpers.test_clocks.create(
            {"frozen_time": int(time.time()), "name": f"{CLOCK_PREFIX}{self.name} {self.tag}"}
        )
        self._customer = self.client.v1.customers.create(
            {
                "test_clock": clock.id,
                "email": f"billing-e2e+{self.tag}@example.com",
                "name": f"billing-e2e {self.name}",
                "metadata": {"billing_e2e_run": self.tag},
            }
        )

    @property
    def started(self) -> bool:
        return self._clock is not None

    @property
    def clock(self) -> stripe.test_helpers.TestClock:
        if self._clock is None:
            raise RuntimeError("The run has not started (or was cleaned up)")
        return self._clock

    @property
    def customer(self) -> stripe.Customer:
        if self._customer is None:
            raise RuntimeError("The run has not started (or was cleaned up)")
        return self._customer

    @property
    def now(self) -> datetime:
        """The simulated time of this run."""
        return datetime.fromtimestamp(self.clock.frozen_time, tz=timezone.utc)

    def advance(self, to: datetime) -> None:
        """Move simulated time forward to `to`, and return once Stripe has processed everything due until then
        (renewals, invoices, payment attempts)."""
        clock = self.clock
        target = int(to.timestamp())
        if target <= clock.frozen_time:
            raise ValueError(f"Cannot go back in time: the clock is already at {self.now:%Y-%m-%d %H:%M} UTC")
        self.client.v1.test_helpers.test_clocks.advance(clock.id, {"frozen_time": target})

        def ready() -> bool:
            self._clock = current = self.client.v1.test_helpers.test_clocks.retrieve(clock.id)
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
        drafts = self.client.v1.invoices.list({"customer": self.customer.id, "status": "draft", "limit": 100})
        due = [inv.automatically_finalizes_at for inv in drafts.data if inv.automatically_finalizes_at]
        if due:
            self.advance(datetime.fromtimestamp(max(due) + 60, tz=timezone.utc))

    def attach_card(self, card: str) -> None:
        """Give the customer one of Stripe's test cards and make it the default for every invoice."""
        if card not in CARDS:
            raise ValueError(f"Unknown test card {card!r}: use one of {', '.join(CARDS)}")
        customer = self.customer
        method = self.client.v1.payment_methods.attach(CARDS[card], {"customer": customer.id})
        self.client.v1.customers.update(customer.id, {"invoice_settings": {"default_payment_method": method.id}})

    def subscribe(self, price: str, params: dict | None = None) -> stripe.Subscription:
        """Subscribe the customer to `price` (a price id, or a lookup key). `params` adds any Stripe subscription
        parameter (trial, coupon, metadata your application expects…). A payment that fails or needs authentication
        leaves the subscription `incomplete`, as on a real checkout, so a scenario can check it."""
        params = dict(params or {})
        # The caller's params are free-form (any Stripe parameter), hence the cast
        create = cast(
            SubscriptionCreateParams,
            {
                **params,
                "customer": self.customer.id,
                "items": [{"price": self.resolve_price(price)}],
                "metadata": {**params.get("metadata", {}), "billing_e2e_run": self.tag},
            },
        )
        subscription = self.client.v1.subscriptions.create(create)
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

    @property
    def period_end(self) -> datetime:
        """End of the subscription's current period (when the next renewal is due)."""
        return datetime.fromtimestamp(self.subscription["items"].data[0].current_period_end, tz=timezone.utc)

    def change_price(self, price: str, params: dict | None = None) -> stripe.Subscription:
        """Move the subscription to another price, with proration unless `params` says otherwise
        (e.g. {"proration_behavior": "always_invoice"} to charge the difference at once)."""
        subscription = self.subscription
        update = cast(
            SubscriptionUpdateParams,
            {
                "proration_behavior": "create_prorations",
                **(params or {}),
                "items": [{"id": subscription["items"].data[0].id, "price": self.resolve_price(price)}],
            },
        )
        return self.client.v1.subscriptions.update(subscription.id, update)

    @property
    def latest_invoice(self) -> stripe.Invoice:
        """The subscription's most recent invoice (a renewal, or the proration invoice of a plan change)."""
        if self.subscription_id is None:
            raise RuntimeError("No subscription yet: subscribe first")
        invoices = self.client.v1.invoices.list({"subscription": self.subscription_id, "limit": 1}).data
        if not invoices:
            raise RuntimeError(f"{self.subscription_id} has no invoice yet")
        return invoices[0]

    def fetch_customer(self) -> stripe.Customer:
        """The customer as Stripe has it now (balance: negative means credit)."""
        return self.client.v1.customers.retrieve(self.customer.id)

    def cancel(self, when: str) -> stripe.Subscription:
        """Cancel the subscription `now`, or at `period_end` (it stays active until then, as from a customer portal)."""
        if self.subscription_id is None:
            raise RuntimeError("No subscription yet: subscribe first")
        if when == "now":
            return self.client.v1.subscriptions.cancel(self.subscription_id)
        if when == "period_end":
            return self.client.v1.subscriptions.update(self.subscription_id, {"cancel_at_period_end": True})
        raise ValueError("cancel takes now or period_end")

    def events(self, event_type: str | None = None) -> list[stripe.Event]:
        """Events about this run's customer since the run started, oldest first.

        Stripe cannot filter events by customer, so this reads the account's recent events and keeps those whose
        object belongs to the run's customer (the customer itself, its subscriptions, invoices, payments…).
        """
        customer_id = self.customer.id
        params: EventListParams = {"created": {"gte": self.started_at}, "limit": 100}
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
        if self._clock is None or self.keep:
            return
        delete_clock(self.client, self._clock.id)
        self._clock = self._customer = None
        self.subscription_id = None
