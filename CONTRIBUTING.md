# Contributing

Thanks for helping! Issues and pull requests are welcome, in English or French.

## Setup

```bash
git clone https://github.com/NKSTUD/billing-e2e && cd billing-e2e
uv sync
uv run pytest
```

Before opening a pull request:

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest
```

## Layout

```
src/billing_e2e/
  safety.py       refuses live keys, confirms test mode with Stripe, masks secrets
  stripe_ops.py   everything that talks to Stripe: StripeRun (clock, customer, cards, subscription, invoices,
                  events, cleanup), deleting clocks
  checks.py       waiting on a state, comparisons, HTTP and command checks
  scenario.py     YAML config and scenarios into validated steps, durations, variables
  runner.py       plays a scenario step by step, hooks, results
  report.py       terminal output and JUnit report
  cli.py          run, list, init, clean
  scenarios/      the built-in scenarios copied by `init`
tests/            pytest; Stripe is faked, except the tests marked as needing a key
examples/         a runnable FastAPI app and Django management commands
```

## Adding a step

1. In `scenario.py`: a frozen dataclass for the step, a `_name(step)` parser that validates it with a clear error,
   and an entry in `STEPS`.
2. In `stripe_ops.py`: the Stripe calls, if the step needs new ones.
3. In `runner.py`: a `case` in `_play` (what it does, and what it returns as detail) and one in `describe` (its label).
4. Tests: parsing in `tests/test_scenario.py`, behavior in `tests/test_runner.py` (with `FakeRun`).
5. Document it in the README steps table and in the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md).

## Rules that keep the tool safe

- Never call Stripe before `check_key` and `confirm_test_mode` have passed.
- Every object a run creates hangs off its test clock (so deleting the clock deletes it) and carries the run's tag.
- Every text that leaves the tool (labels, details, errors, reports) goes through `redact`.
- Wait on a state with `wait_until`, never with a fixed `sleep`.

## Testing against Stripe

Unit tests need no key. The tests that play real scenarios run when a test key and a monthly test price are set:

```bash
BILLING_E2E_STRIPE_KEY=sk_test_... BILLING_E2E_PRICE=price_... uv run pytest
```

Use a test account or a sandbox you can clean: `billing-e2e clean` removes what a run left behind. Never paste a key
in an issue, a commit or a test.

## Commits and pull requests

- One topic per pull request, with a short description of what changes for the user.
- Lint, types and tests must pass (CI runs them on Python 3.10 to 3.13).
