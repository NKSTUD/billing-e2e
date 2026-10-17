# FastAPI example

A minimal FastAPI app with Stripe subscriptions, runnable as is: it records a subscription when Checkout completes, follows it through webhooks (signature checked), and shows the plan on an API. billing-e2e checks it over HTTP.

| File | Role |
|---|---|
| [`app.py`](app.py) | the app: webhook, `GET /accounts/{customer}`, SQLite storage |
| [`billing_e2e_hooks.py`](billing_e2e_hooks.py) | `setup`, `link` (same code as Checkout completion), `teardown` |
| [`billing-e2e.yml`](billing-e2e.yml) | the config |
| [`scenarios/`](scenarios) | renewal, failed renewal, plan change, each checked through the API |

## Run it

You need two monthly prices in your Stripe test account (or sandbox) and the [Stripe CLI](https://docs.stripe.com/stripe-cli).

```bash
pip install fastapi uvicorn stripe billing-e2e
export STRIPE_SECRET_KEY=sk_test_...
export PRO_PRICE=price_... BUSINESS_PRICE=price_...
export PLANS="$PRO_PRICE=Pro,$BUSINESS_PRICE=Business"
export STRIPE_WEBHOOK_SECRET=$(stripe listen --api-key "$STRIPE_SECRET_KEY" --print-secret)

uvicorn app:app --port 8001 &
stripe listen --api-key "$STRIPE_SECRET_KEY" --forward-to localhost:8001/stripe/webhook &
billing-e2e run
```

```
Failed renewal
  ✓ card ok  (1.1 s, clock 2026-10-17)
  ✓ subscribe {monthly_price}  (3.6 s, clock 2026-10-17)
  ✓ expect event invoice.paid  (0.3 s, clock 2026-10-17)
  ✓ check GET /accounts/{customer}  (0.0 s, clock 2026-10-17)
  ✓ card fails_later  (1.0 s, clock 2026-10-17)
  ✓ advance period_end  (18.0 s, clock 2026-11-17)
  ✓ expect event invoice.payment_failed  (4.8 s, clock 2026-11-17)
  ✓ check GET /accounts/{customer}  (0.0 s, clock 2026-11-17)
  ✓ advance 1 month  (47.7 s, clock 2026-12-17)
  ✓ expect event customer.subscription.deleted  (0.3 s, clock 2026-12-17)
  ✓ check GET /accounts/{customer}  (0.0 s, clock 2026-12-17)
```

## In your app

- `link` should call the same function as your `checkout.session.completed` handler (here `activate_subscription`), so the test covers your real code.
- A real app reads the account from the session. Give billing-e2e a test token through `app.headers` (`Authorization: Bearer ${APP_TEST_TOKEN}`) and check `GET /me`, rather than exposing accounts by customer id.
