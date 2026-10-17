"""
A minimal FastAPI app with Stripe subscriptions, to show billing-e2e on something runnable.

It records a subscription when Checkout completes, follows it through webhooks, and shows the plan on an API.
Storage is SQLite to stay short; your app has its own models.

  STRIPE_SECRET_KEY=sk_test_... STRIPE_WEBHOOK_SECRET=whsec_... PLANS="price_123=Pro,price_456=Business" \
  uvicorn app:app --port 8001
"""

import os
import sqlite3
from datetime import datetime, timezone

import stripe
from fastapi import FastAPI, Header, HTTPException, Request

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
PLANS = dict(pair.split("=") for pair in os.environ.get("PLANS", "").split(",") if pair)  # price id -> plan name
DB = os.environ.get("DATABASE", "app.db")

app = FastAPI()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE IF NOT EXISTS accounts (email TEXT PRIMARY KEY, customer TEXT UNIQUE, plan TEXT DEFAULT 'Free',"
        " subscription TEXT, status TEXT, next_billing TEXT)"
    )
    return connection


def activate_subscription(customer: str, subscription_id: str) -> None:
    """What Checkout completion does: the account gets the plan of the subscription's price."""
    subscription = stripe.Subscription.retrieve(subscription_id)
    with db() as connection:
        connection.execute("UPDATE accounts SET subscription = ? WHERE customer = ?", (subscription_id, customer))
    sync(subscription)


def sync(subscription) -> None:
    """Apply the subscription's current state: plan, status, next billing date."""
    item = subscription["items"]["data"][0]
    active = subscription["status"] in ("active", "trialing", "past_due")  # access kept during payment retries
    plan = PLANS.get(item["price"]["id"], "Free") if active else "Free"
    next_billing = datetime.fromtimestamp(item["current_period_end"], tz=timezone.utc).isoformat()
    with db() as connection:
        connection.execute(
            "UPDATE accounts SET plan = ?, status = ?, next_billing = ? WHERE subscription = ?",
            (plan, subscription["status"], next_billing, subscription["id"]),
        )


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    try:
        event = stripe.Webhook.construct_event(
            await request.body(), stripe_signature, os.environ["STRIPE_WEBHOOK_SECRET"]
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(400, "invalid signature") from exc
    obj = event["data"]["object"]
    if event["type"] == "checkout.session.completed" and obj.get("subscription"):
        activate_subscription(obj["customer"], obj["subscription"])
    elif event["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
        # Events can arrive out of order: apply the subscription's current state, not the snapshot
        sync(stripe.Subscription.retrieve(obj["id"]))
    return {"received": True}


@app.get("/accounts/{customer}")
def account(customer: str):
    """Your app would read the account from the session; looked up by customer here to keep the example short."""
    row = db().execute("SELECT * FROM accounts WHERE customer = ?", (customer,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such account")
    return dict(row)
