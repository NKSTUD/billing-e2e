import re
from datetime import datetime, timezone

import pytest
import yaml

from billing_e2e.scenario import (
    Advance,
    Cancel,
    Card,
    ChangePrice,
    CommandCheck,
    Duration,
    ExpectEvent,
    HttpCheck,
    ScenarioError,
    Subscribe,
    SubscriptionIs,
    expand_env,
    fill,
    load_config,
    load_scenario,
    parse_config,
    parse_duration,
    parse_scenario,
)

FAILED_RENEWAL = """
name: A failed renewal ends the subscription
steps:
  - card: ok
  - subscribe: business_monthly
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
  - change_price: premium_monthly
    params: {proration_behavior: always_invoice}
  - cancel: period_end
"""


def test_the_documented_example_parses_into_steps():
    scenario = parse_scenario(yaml.safe_load(FAILED_RENEWAL))
    assert scenario.name == "A failed renewal ends the subscription"
    assert scenario.steps == [
        Card("ok"),
        Subscribe("business_monthly", {"metadata": {"user_email": "billing-e2e+{run}@example.com"}}),
        ExpectEvent("invoice.paid"),
        HttpCheck("GET", "/api/account", {"plan.name": "Business", "subscription.status": "active"}),
        Card("fails_later"),
        Advance("period_end"),
        ExpectEvent("invoice.payment_failed"),
        SubscriptionIs({"status": "past_due"}),
        CommandCheck("python manage.py check_plan --customer $BILLING_E2E_CUSTOMER --plan Business"),
        Advance(Duration(1, "month")),
        ChangePrice("premium_monthly", {"proration_behavior": "always_invoice"}),
        Cancel("period_end"),
    ]


@pytest.mark.parametrize(
    ("steps", "error"),
    [
        ([{"advanse": "1 month"}], "step 1 (advanse): unknown step 'advanse' (did you mean advance?)"),
        ([{"teleport": "x"}], "unknown step 'teleport'; steps are: card, subscribe"),
        ([{"card": "ok"}, {"card": "visa"}], "step 2 (card): unknown card 'visa': use one of ok, declined"),
        ([{"advance": "soon"}], "'soon' is not a duration"),
        ([{"advance": "0 days"}], "'0 days' is not a duration"),
        ([{"cancel": "later"}], "cancel must be now or period_end"),
        ([{"subscribe": "p", "param": {}}], "unexpected key(s): param"),
        ([{"subscription": {}}], "subscription must be a non-empty mapping"),
        ([{"check": {"get": "/a"}}], "expect must be a non-empty mapping"),
        ([{"check": {"get": "/a", "post": "/b", "expect": {"x": 1}}}], "either run"),
        ([{"check": {"run": "ls", "expect": {"x": 1}}}], "unexpected key(s): expect"),
        (["advance 1 month"], "step 1 (?): each step must be a mapping"),
    ],
)
def test_a_bad_step_says_where_and_why(steps, error):
    with pytest.raises(ScenarioError) as bad:
        parse_scenario({"steps": steps}, "renewal.yml")
    assert str(bad.value).startswith("renewal.yml, step")
    assert error in str(bad.value)


@pytest.mark.parametrize("data", [None, [], {"name": "x"}, {"steps": []}])
def test_a_scenario_needs_steps(data):
    with pytest.raises(ScenarioError, match=re.escape("renewal.yml")):
        parse_scenario(data, "renewal.yml")


def test_the_file_name_is_the_default_name(tmp_path):
    path = tmp_path / "renewal.yml"
    path.write_text("steps:\n  - card: ok\n")
    assert load_scenario(path).name == "renewal"


def test_unreadable_files_are_clear_errors(tmp_path):
    with pytest.raises(ScenarioError, match="file not found"):
        load_scenario(tmp_path / "missing.yml")
    broken = tmp_path / "broken.yml"
    broken.write_text("steps: [card: ok")
    with pytest.raises(ScenarioError, match=re.escape("broken.yml: not valid YAML")):
        load_scenario(broken)


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("3 days", datetime(2026, 1, 1), datetime(2026, 1, 4)),
        ("1 hour", datetime(2026, 1, 1), datetime(2026, 1, 1, 1)),
        ("2 weeks", datetime(2026, 1, 1), datetime(2026, 1, 15)),
        ("1 month", datetime(2026, 1, 31), datetime(2026, 2, 28)),
        ("1 month", datetime(2026, 12, 15), datetime(2027, 1, 15)),
        ("13 months", datetime(2026, 1, 15), datetime(2027, 2, 15)),
        ("1 year", datetime(2028, 2, 29), datetime(2029, 2, 28)),
    ],
)
def test_durations_follow_the_calendar(text, start, end):
    start, end = start.replace(tzinfo=timezone.utc), end.replace(tzinfo=timezone.utc)
    assert parse_duration(text).after(start) == end


def test_env_variables_are_expanded_and_a_missing_one_is_an_error():
    assert expand_env({"h": ["Bearer ${TOKEN}"], "n": 3}, {"TOKEN": "abc"}, "c.yml") == {"h": ["Bearer abc"], "n": 3}
    with pytest.raises(ScenarioError, match=re.escape("c.yml: environment variable TOKEN is not set")):
        expand_env("Bearer ${TOKEN}", {}, "c.yml")


def test_run_variables_are_filled_and_other_braces_kept():
    value = {"email": "billing-e2e+{run}@example.com", "list": ["{customer}"], "json": "{not_a_var}", "n": 1}
    assert fill(value, {"run": "a1b2", "customer": "cus_1"}) == {
        "email": "billing-e2e+a1b2@example.com",
        "list": ["cus_1"],
        "json": "{not_a_var}",
        "n": 1,
    }


CONFIG = """
stripe:
  key: ${STRIPE_TEST_KEY}
app:
  base_url: http://localhost:8000
  headers:
    Authorization: Bearer ${APP_TEST_TOKEN}
  setup: python manage.py billing_e2e_user
  teardown: python manage.py billing_e2e_cleanup
"""


def test_the_documented_config_parses_with_env_values():
    env = {"STRIPE_TEST_KEY": "sk_test_123", "APP_TEST_TOKEN": "tok"}
    config = parse_config(yaml.safe_load(CONFIG), "billing-e2e.yml", env)
    assert config.stripe_key == "sk_test_123"
    assert config.base_url == "http://localhost:8000"
    assert config.headers == {"Authorization": "Bearer tok"}
    assert config.setup == "python manage.py billing_e2e_user"
    assert config.teardown == "python manage.py billing_e2e_cleanup"


def test_the_config_needs_a_stripe_key(tmp_path):
    path = tmp_path / "billing-e2e.yml"
    path.write_text("app: {base_url: http://localhost}\n")
    with pytest.raises(ScenarioError, match=re.escape("stripe.key is required")):
        load_config(path)


def test_project_vars_are_read_from_the_config_with_env_values():
    data = {"stripe": {"key": "sk_test_1"}, "vars": {"monthly_price": "${PRICE}", "seats": 3}}
    config = parse_config(data, "billing-e2e.yml", {"PRICE": "price_123"})
    assert config.vars == {"monthly_price": "price_123", "seats": "3"}


def test_project_vars_must_be_a_mapping():
    with pytest.raises(ScenarioError, match="vars must be a mapping"):
        parse_config({"stripe": {"key": "sk_test_1"}, "vars": ["price_123"]}, "billing-e2e.yml", {})


def test_every_builtin_scenario_is_valid():
    from billing_e2e.cli import BUILTIN

    names = sorted(f.name for f in BUILTIN.iterdir() if f.name.endswith(".yml"))
    assert names == [
        "cancel-at-period-end.yml",
        "failed-renewal.yml",
        "plan-change.yml",
        "renewal.yml",
        "signup-card-problems.yml",
    ]
    for name in names:
        scenario = parse_scenario(yaml.safe_load((BUILTIN / name).read_text()), name)
        assert scenario.steps


def test_invoice_and_customer_steps_parse_with_comparisons():
    from billing_e2e.scenario import CustomerIs, InvoiceIs

    steps = [{"invoice": {"total": {"lt": 0}}}, {"customer": {"balance": {"lt": 0}}}]
    assert parse_scenario({"steps": steps}).steps == [
        InvoiceIs({"total": {"lt": 0}}),
        CustomerIs({"balance": {"lt": 0}}),
    ]
