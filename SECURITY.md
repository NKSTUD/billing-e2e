# Security

## Reporting a vulnerability

Please do not open a public issue. Use [GitHub private vulnerability reporting](https://github.com/NKSTUD/billing-e2e/security/advisories/new) and include the steps to reproduce. You will get an answer within a week.

## What billing-e2e does with your Stripe account

- It only runs with a test key (`sk_test_`, `rk_test_`). Live and publishable keys are refused before anything is
  sent, and Stripe is asked to confirm the key is a working test key.
- It creates objects only under its own test clocks, named `billing-e2e …`: a customer, its payment methods,
  subscriptions and invoices. It never reads, changes or deletes anything else, except clocks named `billing-e2e …`
  when you run `billing-e2e clean`.
- It deletes each scenario's clock at the end, unless you pass `--keep`.

## Your keys

- Keys are read from environment variables (`${NAME}` in the config) and only sent to Stripe.
- Anything that looks like a Stripe secret (`sk_`, `rk_`, `whsec_`) is masked in the output and the reports.
- Nothing is sent anywhere else: no telemetry, no server of ours.
- A restricted test key with only the permissions listed in the README limits what a leaked key can do. If a key
  leaks, roll it in the Stripe Dashboard (Developers › API keys).

## Commands from your config

`setup`, `after_subscribe`, `teardown`, `clean` and `check: run` run through your shell, like a Makefile. Only run
configs and scenarios you trust.
