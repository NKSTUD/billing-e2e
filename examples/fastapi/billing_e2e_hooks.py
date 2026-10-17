"""
billing-e2e hooks for the example app: python billing_e2e_hooks.py setup|link|teardown

billing-e2e passes the run through the environment. Refuses a live key, and only touches billing-e2e+…@example.com.
"""

import os
import sys

from app import activate_subscription, db, stripe

if not stripe.api_key.startswith(("sk_test_", "rk_test_")):
    sys.exit("Live Stripe key: billing-e2e only runs in test mode")
email = os.environ["BILLING_E2E_CUSTOMER_EMAIL"]
if not (email.startswith("billing-e2e+") and email.endswith("@example.com")):
    sys.exit(f"{email} is not a billing-e2e test user")

action = sys.argv[1]
with db() as connection:
    if action == "setup":
        connection.execute(
            "INSERT INTO accounts (email, customer) VALUES (?, ?)", (email, os.environ["BILLING_E2E_CUSTOMER"])
        )
    elif action == "teardown":
        connection.execute("DELETE FROM accounts WHERE email = ?", (email,))
if action == "link":
    subscription = stripe.Subscription.retrieve(os.environ["BILLING_E2E_SUBSCRIPTION"])
    # Checkout only completes once the first payment went through: without it, nothing to record
    if subscription.status in ("active", "trialing"):
        activate_subscription(os.environ["BILLING_E2E_CUSTOMER"], subscription.id)
