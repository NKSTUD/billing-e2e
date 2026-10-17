# billing-e2e

[![CI](https://github.com/NKSTUD/billing-e2e/actions/workflows/ci.yml/badge.svg)](https://github.com/NKSTUD/billing-e2e/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

End-to-end tests for Stripe subscriptions. Renewals, failed payments, retries, plan changes with proration, cancellations: **forty days of billing in a minute**, checked against your own application.

```
Failed renewal
  ✓ card ok  (1.0 s, clock 2026-10-16)
  ✓ subscribe {monthly_price}  (2.3 s, clock 2026-10-16)
  ✓ expect event invoice.paid  (0.3 s, clock 2026-10-16)
  ✓ card fails_later  (1.0 s, clock 2026-10-16)
  ✓ advance period_end  (16.0 s, clock 2026-11-17)
  ✓ expect event invoice.payment_failed  (0.3 s, clock 2026-11-17)
  ✓ subscription status = past_due  (0.3 s, clock 2026-11-17)
  ✓ check GET /api/account  (0.4 s, clock 2026-11-17)
  ✓ advance 1 month  (14.9 s, clock 2026-12-17)
  ✓ expect event customer.subscription.deleted  (0.3 s, clock 2026-12-17)
  ✓ check GET /api/account  (0.4 s, clock 2026-12-17)

1 scenario(s) passed
```

> Community project, not affiliated with or endorsed by Stripe.

## Why

Billing code breaks quietly. A renewal that does not update the next billing date, a failed payment that keeps paid access forever, a downgrade charged at full price: nothing crashes, and you find out from a customer or from your revenue.

Testing it by hand is slow. You advance a [test clock](https://docs.stripe.com/billing/testing/test-clocks), wait for the draft invoice to be finalized about an hour later (in simulated time), resend events, check your database, and start again for every case. Most teams do it once, before launch, and never again.

billing-e2e turns each case into a short scenario that runs in a minute, locally or in CI:

- it creates a customer on its own test clock, subscribes it, moves time forward and lets Stripe do what it does in production: finalize invoices, charge the card, retry, cancel;
- it waits for the events your webhooks receive;
- it checks **your application** at every step, through its API or a command, so you know your webhooks did their job.

## Safety

- **Test mode only.** Live keys (`sk_live_`, `rk_live_`) are refused before anything is sent, and Stripe is asked to confirm the key is a working test key. There is no flag to turn this off.
- **Nothing touches your existing data.** Every scenario creates its own test clock and customer, tagged `billing-e2e`, and deletes them at the end, including after a failure or Ctrl+C (unless you ask to keep them with `--keep`). Deleting a test clock deletes everything attached to it.
- **Nothing leaves your machine.** No telemetry, no server of ours. Keys come from environment variables and never appear in the output or in reports: anything that looks like a Stripe secret is masked (`sk_test_…`).
- **Every file is checked before the first call to Stripe.** A typo in a scenario stops the run with the file, the step and a suggestion, not halfway through.
- Small and readable: three dependencies (`stripe`, `httpx`, `pyyaml`).

A [restricted key](https://docs.stripe.com/keys#limit-access) (`rk_test_…`) with write access to Customers, Payment methods, Subscriptions, Invoices and Test clocks, and read access to Prices and Events, is enough.

## Quick start

```bash
pip install billing-e2e     # or: uv tool install billing-e2e
billing-e2e init            # writes billing-e2e.yml and the built-in scenarios in scenarios/
```

Put your prices in `billing-e2e.yml`:

```yaml
vars:
  monthly_price: price_1Abc...      # a price id, or a lookup key
  upgrade_price: price_1Def...      # a more expensive monthly price
```

Then run, with your application running and receiving webhooks (`stripe listen --forward-to localhost:8000/your/webhook/`):

```bash
export STRIPE_TEST_KEY=sk_test_...
billing-e2e run                     # every scenario in scenarios/
billing-e2e run scenarios/renewal.yml --junit report.xml
```

`billing-e2e list` checks the scenarios without running them.

### Inspecting a run

Everything a scenario creates is deleted at the end. To look at it in the Stripe Dashboard (the invoices, the proration, the timeline), keep it:

```bash
billing-e2e run scenarios/plan-change.yml --keep
#   kept for inspection: clock clock_1…, customer cus_…, subscription sub_…
#   https://dashboard.stripe.com/test/customers/cus_…
billing-e2e clean        # deletes every test clock billing-e2e created, and everything attached to it
```

`--keep` also skips `teardown`, so your application's test data stays too. `clean` only deletes clocks named `billing-e2e …`, in test mode; it also removes what a killed run left behind.

## Built-in scenarios

`init` copies them into `scenarios/` so you can read and adapt them. They check Stripe's side; each one has commented examples of checks on your application.

| Scenario | What it covers |
|---|---|
| `renewal.yml` | the monthly payment goes through and the subscription stays active |
| `failed-renewal.yml` | the card stops working: renewal fails, `past_due`, retries run out, the subscription ends |
| `plan-change.yml` | upgrade mid-period: a proration invoice is paid at once; downgrade: the unused time becomes a credit |
| `cancel-at-period-end.yml` | cancellation keeps access until the end of the paid period |
| `signup-card-problems.yml` | a declined card, and a 3D Secure payment never confirmed: no access is granted |

The end of `failed-renewal.yml` follows your account's setting for exhausted retries (Billing › Revenue recovery). It assumes "cancel the subscription"; with "mark as unpaid", expect `status: unpaid` instead.

## Writing a scenario

```yaml
name: Failed renewal
steps:
  - card: ok
  - subscribe: "{monthly_price}"
    params: {metadata: {user_email: "billing-e2e+{run}@example.com"}}
  - expect_event: invoice.paid
  - check:
      get: /api/account
      expect: {plan.name: Business, subscription.status: active}

  - card: fails_later
  - advance: period_end
  - expect_event: invoice.payment_failed
  - subscription: {status: past_due}
  - check:
      run: python manage.py check_plan --customer $BILLING_E2E_CUSTOMER --plan Business

  - advance: 1 month
  - expect_event: customer.subscription.deleted
  - check:
      get: /api/account
      expect: {plan.name: Free}
```

### Steps

| Step | What it does |
|---|---|
| `card: ok \| declined \| fails_later \| 3ds` | gives the customer a Stripe test card and makes it the default |
| `subscribe: <price>` (+ `params:`) | subscribes the customer; `params` adds any [subscription parameter](https://docs.stripe.com/api/subscriptions/create) (trial, coupon, metadata) |
| `advance: <duration> \| period_end` | moves the clock forward (`3 days`, `2 weeks`, `1 month`…), then lets Stripe finalize and charge the invoices that are due |
| `expect_event: <type>` | waits for the next event of that type about this customer |
| `subscription: {field: value}` | checks the subscription on Stripe's side (`status`, `cancel_at_period_end`, `items.data.0.price.id`…) |
| `invoice: {field: value}` | checks the subscription's latest invoice (`billing_reason`, `status`, `amount_paid`, `total`…) |
| `customer: {field: value}` | checks the customer (`balance`: negative means credit) |
| `check: {get: <path>, expect: {...}}` | calls your API (also `post:`) and compares fields of the JSON answer |
| `check: {run: <command>}` | runs a command of your application; passes when it exits with 0 |
| `change_price: <price>` (+ `params:`) | moves the subscription to another price, with proration (`proration_behavior: always_invoice` charges the difference at once) |
| `cancel: now \| period_end` | cancels immediately, or at the end of the period |

The four test cards:

| Card | Behavior |
|---|---|
| `ok` | every payment succeeds |
| `declined` | refused as soon as it is added (the step passes when Stripe refuses it) |
| `fails_later` | added fine, then every payment fails: subscribe with `ok`, then switch to it to make a renewal fail |
| `3ds` | every payment needs authentication, which a test never gives: the subscription stays `incomplete` |

### Checks wait for your application

Your application handles webhooks a moment after Stripe sends them, so every `check` is retried for up to 30 seconds. When it still fails, the report shows what your application last answered:

```
✗ check GET /api/account  (30.4 s, clock 2026-11-17)
    GET /api/account: still failing after 30 s. plan.name is 'Free', expected 'Business'
```

Fields are read with dotted paths: `plan.name`, `credits.0.remaining`. A value is compared for equality, or with `gt`, `gte`, `lt`, `lte`, `ne`:

```yaml
- invoice: {billing_reason: subscription_update, status: paid, amount_paid: {gt: 0}}
- customer: {balance: {lt: 0}}                  # the downgrade left a credit
- check: {get: /api/account, expect: {credits.0.remaining: {gte: 1}}}
```

### Variables

- `${NAME}` is read from the environment when the config is loaded. Use it for secrets; a missing variable stops the run.
- `{name}` is replaced when the scenario plays: your `vars:` (prices), and `{run}`, `{customer}`, `{subscription}` for the current run.

## Configuration

`billing-e2e.yml`, read from the current directory (or `--config <file>`):

```yaml
stripe:
  key: ${STRIPE_TEST_KEY}

app:
  base_url: http://localhost:8000          # for check: get/post
  headers:
    Authorization: Bearer ${APP_TEST_TOKEN}
  setup: python manage.py billing_e2e setup               # before each scenario
  after_subscribe: python manage.py billing_e2e link      # after each subscribe step
  teardown: python manage.py billing_e2e teardown         # after each scenario, even a failed one

vars:
  monthly_price: price_1Abc...
  upgrade_price: price_1Def...
```

Commands (`setup`, `after_subscribe`, `teardown` and `check: run`) get the current run in their environment:

| Variable | Value |
|---|---|
| `BILLING_E2E_RUN` | a short id, unique to the run |
| `BILLING_E2E_CUSTOMER` | the Stripe customer id (`cus_…`) |
| `BILLING_E2E_CUSTOMER_EMAIL` | `billing-e2e+<run>@example.com` |
| `BILLING_E2E_SUBSCRIPTION` | the subscription id (`sub_…`), once subscribed |

### Apps that use Stripe Checkout

Most applications create their local subscription when they receive `checkout.session.completed`. billing-e2e creates subscriptions through the API (a Checkout page cannot be paid without a browser), so that event never comes.

Use the hooks to do what your Checkout handler does:

- `setup` creates a test user and links it to `BILLING_E2E_CUSTOMER`;
- `after_subscribe` records `BILLING_E2E_SUBSCRIPTION` for that user, ideally by calling the same code as your Checkout handler. Skip it when the subscription is not `active`: Checkout only completes once the first payment went through;
- `teardown` deletes the test user.

Every later event (`customer.subscription.updated`, `customer.subscription.deleted`, `invoice.*`) reaches your webhook as usual.

Examples, checks included:

- [examples/fastapi](examples/fastapi): a small FastAPI app you can run as is, checked over HTTP;
- [examples/django](examples/django): management commands for a Django app, checked through a command.

billing-e2e does not depend on a framework: anything that receives Stripe webhooks and can be checked over HTTP or by a command works (Laravel, Rails, Node…).

## In CI

```yaml
# .github/workflows/billing.yml
jobs:
  billing-e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # start your application and forward webhooks to it (stripe listen --forward-to …)
      - run: pip install billing-e2e
      - run: billing-e2e run --junit billing-e2e.xml
        env:
          STRIPE_TEST_KEY: ${{ secrets.STRIPE_TEST_KEY }}
```

Exit codes: `0` every scenario passed, `1` a scenario failed, `2` the run could not start (config, key, scenario file), `130` interrupted.

## How it works

- **Time.** Each scenario runs on its own [test clock](https://docs.stripe.com/billing/testing/test-clocks). `advance` moves it, then waits until Stripe reports the clock ready: renewals, invoices and payment attempts due by then are done. Nothing waits a fixed delay.
- **Invoices.** A renewal invoice stays a draft for about an hour before Stripe finalizes and charges it. Rather than forcing that through the API, which would skip what Stripe really does (retries included), `advance` moves the clock just past the moment Stripe planned to finalize the drafts.
- **Events** are read from Stripe's [Events API](https://docs.stripe.com/api/events/list) and filtered to the run's customer, so the tool needs no open port.
- **Cleanup** deletes the test clock, which deletes its customer and subscriptions. If the clock is still advancing (Ctrl+C mid-step), cleanup waits for it, since Stripe refuses to delete a moving clock.

## Limits

- Stripe's test clock rules apply: a clock holds at most three customers (billing-e2e uses one), and one `advance` cannot go further than two billing intervals of the shortest subscription. Split long jumps (`advance: 1 month` twice rather than `2 months` on a monthly plan).
- Subscriptions are created through the API, not through a Checkout page or a Payment Link; see [Apps that use Stripe Checkout](#apps-that-use-stripe-checkout).
- One price per subscription for now.
- Amounts are checked against bounds (`amount_paid: {gt: 0}`), not recomputed: Stripe computes the proration to the second, and what an app gets wrong is usually whether proration happens at all.

## Development

```bash
uv sync
uv run pytest                       # unit tests
uv run ruff check . && uv run pyright
```

Tests against a real Stripe test account run when these are set:

```bash
BILLING_E2E_STRIPE_KEY=sk_test_... BILLING_E2E_PRICE=price_... uv run pytest
```

## License

MIT
