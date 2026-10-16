import sys

import httpx
import pytest

from billing_e2e.checks import MISSING, CheckFailed, Result, command_check, eventually, http_check, lookup, wait_until


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr("billing_e2e.checks.time.sleep", lambda _s: None)


def test_wait_until_returns_the_first_truthy_result():
    answers = iter([None, 0, "ready"])
    assert wait_until(lambda: next(answers), what="x", timeout=5, interval=0) == "ready"


def test_wait_until_gives_up_with_a_clear_message():
    with pytest.raises(TimeoutError, match="Gave up waiting for the clock after 0 s"):
        wait_until(lambda: False, what="the clock", timeout=0, interval=0)


def test_eventually_retries_until_the_application_catches_up():
    answers = iter([Result(False, "plan is 'Free'"), Result(True, "all fields match")])
    assert eventually(lambda: next(answers), what="plan", timeout=5).passed


def test_eventually_fails_with_the_last_answer():
    with pytest.raises(CheckFailed, match=r"plan: still failing after 0 s\. plan is 'Free'"):
        eventually(lambda: Result(False, "plan is 'Free'"), what="plan", timeout=0)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("plan.name", "Business"),
        ("credits.0.remaining", 20),
        ("credits.-1.name", "Minutes"),
        ("active", False),
        ("plan.price", MISSING),
        ("credits.5.name", MISSING),
        ("plan.name.first", MISSING),
    ],
)
def test_lookup_reads_dotted_paths(path, expected):
    data = {"plan": {"name": "Business"}, "credits": [{"remaining": 20}, {"name": "Minutes"}], "active": False}
    found = lookup(data, path)
    assert found is MISSING if expected is MISSING else found == expected


def app(status=200, body=None, text=None):
    """An application answering one canned response, recording the requests it gets."""
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body) if text is None else httpx.Response(status, text=text)

    return httpx.Client(transport=httpx.MockTransport(handle), base_url="http://app.test"), seen


def test_http_check_passes_when_every_field_matches_and_sends_the_headers():
    client, seen = app(body={"plan": {"name": "Business"}, "active": True})
    expect = {"plan.name": "Business", "active": True}
    result = http_check(client, "GET", "/api/account", expect, {"Authorization": "x"})
    assert result.passed
    assert seen[0].url.path == "/api/account"
    assert seen[0].headers["Authorization"] == "x"


def test_http_check_lists_every_wrong_or_missing_field():
    client, _ = app(body={"plan": {"name": "Free"}})
    result = http_check(client, "GET", "/api/account", {"plan.name": "Business", "next_billing": "2026-11-16"})
    assert not result.passed
    assert result.detail == "plan.name is 'Free', expected 'Business'; next_billing is missing"


@pytest.mark.parametrize(
    ("status", "text", "detail"),
    [
        (500, "boom with sk_live_51Secret", "answered 500: boom with sk_live_…"),
        (200, "<html>not json</html>", "did not answer JSON"),
    ],
)
def test_http_check_explains_a_bad_answer_without_leaking_secrets(status, text, detail):
    client, _ = app(status=status, text=text)
    result = http_check(client, "GET", "/api/account", {"plan.name": "Business"})
    assert not result.passed
    assert detail in result.detail
    assert "51Secret" not in result.detail


def test_http_check_reports_an_unreachable_application():
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://app.test")
    result = http_check(client, "GET", "/api/account", {})
    assert not result.passed
    assert "ConnectError" in result.detail


def python(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def test_command_check_passes_on_exit_0_and_gets_the_run_environment():
    command = python("import os; print(os.environ['BILLING_E2E_CUSTOMER'])")
    result = command_check(command, {"BILLING_E2E_CUSTOMER": "cus_1"})
    assert result == Result(True, "cus_1")


def test_command_check_fails_with_the_exit_code_and_output_without_secrets():
    result = command_check(python("import sys; print('plan is Free, key sk_test_51Secret'); sys.exit(3)"))
    assert not result.passed
    assert result.detail.endswith("exited with 3: plan is Free, key sk_test_…")


def test_command_check_stops_a_command_that_hangs():
    result = command_check(python("import time; time.sleep(5)"), timeout=0.5)
    assert not result.passed
    assert "did not finish within 0.5 s" in result.detail
