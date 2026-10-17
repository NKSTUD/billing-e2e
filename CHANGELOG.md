# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0]

### Added

- `billing-e2e run` plays scenarios on Stripe test clocks, one isolated customer and clock per scenario, deleted at
  the end (also after a failure or Ctrl+C). Live output, JUnit report (`--junit`), exit codes 0/1/2/130.
- Steps: `card` (ok, declined, fails_later, 3ds), `subscribe`, `advance` (a duration or `period_end`, with invoices
  finalized and charged the way Stripe does it), `expect_event`, `subscription`, `invoice`, `customer`, `check`
  (HTTP or command, retried until the app catches up), `change_price` (with proration), `cancel`.
- Comparisons in checks: `gt`, `gte`, `lt`, `lte`, `ne`, besides equality.
- Five built-in scenarios copied by `billing-e2e init`: renewal, failed renewal, plan change with proration,
  cancellation at period end, card problems at signup.
- Config hooks: `setup`, `after_subscribe` (for apps that record subscriptions from `checkout.session.completed`),
  `teardown`, `clean`; project `vars` such as prices.
- `run --keep` leaves a scenario's objects for inspection, with a Dashboard link; `billing-e2e clean` deletes every
  clock billing-e2e created and runs the app's `clean` command.
- Safety: live and publishable keys refused before any call, test mode confirmed with Stripe, every file checked
  before the first call, Stripe secrets masked in every output.
- Examples: a runnable FastAPI app checked over HTTP, and Django management commands.

### Fixed

- A Stripe error while creating or deleting a scenario's clock or customer fails that scenario with a clear message,
  and the next scenarios still run (it used to stop the whole run with a traceback and no JUnit report).
- Ctrl+C with `--keep` no longer claims the current scenario's test clock was deleted.

### Changed

- `stripe` is capped below 17: each major version pins a newer Stripe API version, which can move fields.
