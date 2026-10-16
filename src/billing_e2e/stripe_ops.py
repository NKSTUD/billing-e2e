"""Everything that talks to Stripe: test clock, customer, subscription, invoices, test cards, events, cleanup."""

import time
import uuid
from datetime import datetime, timezone

import stripe

from billing_e2e.checks import wait_until

# Stripe usually finishes advancing a clock in seconds; a clock with many objects can take a few minutes
ADVANCE_TIMEOUT = 300


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

    def __enter__(self) -> "StripeRun":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.cleanup()

    def start(self) -> None:
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

    def cleanup(self) -> None:
        if self.clock is None:
            return
        try:
            self.client.v1.test_helpers.test_clocks.delete(self.clock.id)
        except stripe.InvalidRequestError as exc:
            if exc.code != "resource_missing":  # already gone is fine
                raise
        self.clock = self.customer = None

    def _clock(self) -> stripe.test_helpers.TestClock:
        if self.clock is None:
            raise RuntimeError("The run has not started (or was cleaned up)")
        return self.clock
