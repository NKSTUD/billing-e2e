"""
Links billing-e2e scenarios to a Django app that records subscriptions from checkout.session.completed.

Copy to <your_app>/management/commands/billing_e2e.py and adapt the three functions marked ADAPT to your models.

  setup     creates the scenario's test user, linked to the Stripe customer
  link      records the subscription the scenario created, as your Checkout handler would
  expect    checks the plan your app gives the user (exit code 1 otherwise)
  teardown  deletes the test user
  clean     deletes every billing-e2e test user (run by `billing-e2e clean`, after --keep runs)

billing-e2e passes the run through the environment (BILLING_E2E_CUSTOMER, BILLING_E2E_CUSTOMER_EMAIL,
BILLING_E2E_SUBSCRIPTION). Refuses a live Stripe key, and only touches billing-e2e+…@example.com users.
"""

import os

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


# ADAPT 1: create a user your app can subscribe, linked to the Stripe customer
def create_test_user(email: str, customer_id: str) -> None:
    user = get_user_model().objects.create_user(email=email, password=None)
    user.profile.stripe_customer_id = customer_id  # wherever you store it
    user.profile.save()


# ADAPT 2: what your checkout.session.completed handler does, ideally by calling the same function
def record_subscription(email: str, subscription: stripe.Subscription) -> None:
    from billing.handlers import activate_subscription  # your code

    activate_subscription(user=get_user_model().objects.get(email=email), subscription_id=subscription.id)


# ADAPT 3: the plan your app currently gives the user, as shown to them
def current_plan(email: str) -> str:
    return get_user_model().objects.get(email=email).profile.plan.name


def run_value(name: str) -> str:
    if not (value := os.environ.get(name)):
        raise CommandError(f"{name} is missing: this command is run by billing-e2e")
    return value


class Command(BaseCommand):
    help = "Links billing-e2e scenarios to this app (setup, link, expect, teardown); Stripe test keys only."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["setup", "link", "expect", "teardown", "clean"])
        parser.add_argument("--plan", help="expect: the plan the user should have")

    def handle(self, *args, **options):
        if not settings.STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_")):
            raise CommandError("Live Stripe key: billing-e2e only runs in test mode")
        if options["action"] == "clean":
            users = get_user_model().objects
            test_users = users.filter(email__startswith="billing-e2e+", email__endswith="@example.com")
            self.stdout.write(f"{test_users.delete()[0]} test object(s) deleted")
            return
        email = run_value("BILLING_E2E_CUSTOMER_EMAIL")
        if not (email.startswith("billing-e2e+") and email.endswith("@example.com")):
            raise CommandError(f"{email} is not a billing-e2e test user")

        if options["action"] == "setup":
            create_test_user(email, run_value("BILLING_E2E_CUSTOMER"))
        elif options["action"] == "link":
            stripe.api_key = settings.STRIPE_SECRET_KEY
            subscription = stripe.Subscription.retrieve(run_value("BILLING_E2E_SUBSCRIPTION"))
            # Checkout only completes once the first payment went through: without it, nothing to record
            if subscription.status in ("active", "trialing"):
                record_subscription(email, subscription)
        elif options["action"] == "expect":
            if options["plan"] and (plan := current_plan(email)) != options["plan"]:
                raise CommandError(f"plan is {plan}, expected {options['plan']}")
        else:
            get_user_model().objects.filter(email=email).delete()
