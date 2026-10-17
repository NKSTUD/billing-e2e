"""Command line: `run` plays scenarios, `list` checks them, `init` writes a config, `clean` deletes kept clocks."""

import argparse
import sys
from importlib.resources import files
from pathlib import Path

import stripe

import billing_e2e
from billing_e2e.checks import command_check
from billing_e2e.report import Console, write_junit
from billing_e2e.runner import Runner
from billing_e2e.safety import UnsafeKeyError, check_key, confirm_test_mode
from billing_e2e.scenario import Config, Scenario, ScenarioError, load_config, load_scenario
from billing_e2e.stripe_ops import delete_all_clocks

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
  # If your app records a subscription from checkout.session.completed, record the one each scenario creates
  # through the API (gets BILLING_E2E_SUBSCRIPTION):
  # after_subscribe: python manage.py billing_e2e_link
  # Run by `billing-e2e clean`: delete the app's test data that --keep left behind
  # clean: python manage.py billing_e2e_clean_all

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
        client = stripe_client(args)
    except (ScenarioError, UnsafeKeyError) as exc:
        print(f"billing-e2e: {exc}", file=sys.stderr)
        return UNUSABLE

    console, results = Console(), []
    runner = Runner(config, client, keep=args.keep)
    try:
        for scenario in scenarios:
            console.scenario(scenario.name)
            result = runner.run(scenario, on_step=console.step)
            console.outcome(result)
            results.append(result)
    except KeyboardInterrupt:
        left = "kept: delete it with billing-e2e clean" if args.keep else "deleted"
        print(f"\nbilling-e2e: interrupted; the test clock of the current scenario was {left}", file=sys.stderr)
        return INTERRUPTED
    finally:
        if args.junit and results:
            write_junit(results, args.junit)
    console.summary(results)
    if args.keep:
        print("Test clocks kept (Stripe Dashboard > Billing > Test clocks). Delete them with: billing-e2e clean")
    return 0 if all(r.passed for r in results) else FAILED


def stripe_client(args: argparse.Namespace, config: Config | None = None) -> stripe.StripeClient:
    """A client for a test key only, confirmed with Stripe."""
    config = config or load_config(args.config)
    check_key(config.stripe_key)
    client = stripe.StripeClient(config.stripe_key, max_network_retries=2)
    confirm_test_mode(client)
    return client


def clean(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        client = stripe_client(args, config)
    except (ScenarioError, UnsafeKeyError) as exc:
        print(f"billing-e2e: {exc}", file=sys.stderr)
        return UNUSABLE
    deleted = delete_all_clocks(client)
    for name in deleted:
        print(f"deleted {name}")
    print(f"{len(deleted)} billing-e2e test clock(s) deleted" if deleted else "no billing-e2e test clock left")
    if not config.clean:
        return 0
    outcome = command_check(config.clean)  # the app's own test data (kept by --keep, or left by a killed run)
    print(f"app.clean: {outcome.detail}" if outcome.passed else f"billing-e2e: app.clean failed: {outcome.detail}")
    return 0 if outcome.passed else FAILED


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
    run_parser.add_argument(
        "--keep",
        action="store_true",
        help="keep each scenario's test clock, customer and subscription to inspect them (no teardown either); "
        "delete them later with `billing-e2e clean`",
    )
    run_parser.set_defaults(handler=run)

    list_parser = commands.add_parser("list", help="check and list the scenarios, without running them")
    list_parser.add_argument("paths", nargs="*", default=["scenarios"])
    list_parser.set_defaults(handler=list_scenarios)

    commands.add_parser("init", help="write a first config and scenario").set_defaults(handler=init)
    commands.add_parser(
        "clean", help="delete every test clock billing-e2e created (kept with --keep, or left by a killed run)"
    ).set_defaults(handler=clean)

    args = parser.parse_args(argv)
    sys.exit(args.handler(args))
