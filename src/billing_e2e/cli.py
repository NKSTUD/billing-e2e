"""Command line: `run` plays scenarios, `list` shows what a run would play, `init` writes a first config."""

import argparse
import sys
from importlib.resources import files
from pathlib import Path

import stripe

import billing_e2e
from billing_e2e.report import Console, write_junit
from billing_e2e.runner import Runner
from billing_e2e.safety import UnsafeKeyError, check_key, confirm_test_mode
from billing_e2e.scenario import Scenario, ScenarioError, load_config, load_scenario

BUILTIN = files("billing_e2e") / "scenarios"

# Exit codes: 0 all passed, 1 a scenario failed, 2 the run could not start, 130 interrupted
FAILED, UNUSABLE, INTERRUPTED = 1, 2, 130

CONFIG_TEMPLATE = """\
# billing-e2e configuration. Secrets come from the environment: never write them here.
stripe:
  key: ${STRIPE_TEST_KEY}          # a sk_test_ or rk_test_ key; live keys are refused

app:
  base_url: http://localhost:8000  # for check: get/post
  # headers:
  #   Authorization: Bearer ${APP_TEST_TOKEN}
  # Commands run before and after each scenario. They get BILLING_E2E_RUN, BILLING_E2E_CUSTOMER,
  # BILLING_E2E_CUSTOMER_EMAIL and BILLING_E2E_SUBSCRIPTION in their environment.
  # setup: python manage.py billing_e2e_user
  # teardown: python manage.py billing_e2e_cleanup

# Your values, used as {name} in the scenarios: a price id (price_...) or a lookup key
vars:
  monthly_price: price_your_monthly_plan
  upgrade_price: price_a_more_expensive_monthly_plan
"""


def find_scenarios(paths: list[str]) -> list[Path]:
    """Files as given; a directory contributes its *.yml and *.yaml files, sorted."""
    found = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found += sorted(p for p in path.iterdir() if p.suffix in (".yml", ".yaml"))
        elif path.exists():
            found.append(path)
        else:
            raise ScenarioError(f"{raw}: no such file or directory")
    if not found:
        raise ScenarioError(f"no scenario found in {', '.join(paths)}")
    return found


def load_all(paths: list[str]) -> list[Scenario]:
    """Every scenario is checked before anything is sent to Stripe."""
    return [load_scenario(path) for path in find_scenarios(paths)]


def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        scenarios = load_all(args.paths)
        check_key(config.stripe_key)
        client = stripe.StripeClient(config.stripe_key, max_network_retries=2)
        confirm_test_mode(client)
    except (ScenarioError, UnsafeKeyError) as exc:
        print(f"billing-e2e: {exc}", file=sys.stderr)
        return UNUSABLE

    console, results = Console(), []
    runner = Runner(config, client)
    try:
        for scenario in scenarios:
            console.scenario(scenario.name)
            result = runner.run(scenario, on_step=console.step)
            console.outcome(result)
            results.append(result)
    except KeyboardInterrupt:
        print("\nbilling-e2e: interrupted; the test clock of the current scenario was deleted", file=sys.stderr)
        return INTERRUPTED
    finally:
        if args.junit and results:
            write_junit(results, args.junit)
    console.summary(results)
    return 0 if all(r.passed for r in results) else FAILED


def list_scenarios(args: argparse.Namespace) -> int:
    try:
        scenarios = load_all(args.paths)
    except ScenarioError as exc:
        print(f"billing-e2e: {exc}", file=sys.stderr)
        return UNUSABLE
    for scenario in scenarios:
        print(f"{scenario.name}  ({len(scenario.steps)} steps)")
    return 0


def init(args: argparse.Namespace) -> int:
    """Write the config and copy the built-in scenarios, to read and adapt. Existing files are kept."""
    targets = {Path(args.config): CONFIG_TEMPLATE}
    for scenario in sorted(BUILTIN.iterdir(), key=lambda f: f.name):
        if scenario.name.endswith(".yml"):
            targets[Path("scenarios") / scenario.name] = scenario.read_text(encoding="utf-8")
    for path, content in targets.items():
        if path.exists():
            print(f"kept {path} (already there)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path}")
    print(f"Next: set STRIPE_TEST_KEY, put your prices under vars: in {args.config}, then run: billing-e2e run")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="billing-e2e", description=billing_e2e.__doc__)
    parser.add_argument("--config", default="billing-e2e.yml", help="config file (default: billing-e2e.yml)")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="play scenarios on Stripe test clocks")
    run_parser.add_argument("paths", nargs="*", default=["scenarios"], help="scenario files or directories")
    run_parser.add_argument("--junit", metavar="FILE", help="also write a JUnit XML report (for CI)")
    run_parser.set_defaults(handler=run)

    list_parser = commands.add_parser("list", help="check and list the scenarios, without running them")
    list_parser.add_argument("paths", nargs="*", default=["scenarios"])
    list_parser.set_defaults(handler=list_scenarios)

    commands.add_parser("init", help="write a first config and scenario").set_defaults(handler=init)

    args = parser.parse_args(argv)
    sys.exit(args.handler(args))
