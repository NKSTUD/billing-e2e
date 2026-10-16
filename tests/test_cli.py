import io
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest

from billing_e2e import cli
from billing_e2e.cli import main
from billing_e2e.report import Console, write_junit
from billing_e2e.runner import RunResult, StepResult

CLOCK = datetime(2026, 11, 17, tzinfo=timezone.utc)


def exit_code(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as done:
        main(argv)
    return int(done.value.code or 0)


def test_help_lists_the_commands(capsys):
    assert exit_code(["--help"]) == 0
    out = capsys.readouterr().out
    assert all(command in out for command in ("run", "list", "init"))


def test_init_writes_a_config_and_a_scenario_and_keeps_existing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert exit_code(["init"]) == 0
    assert "${STRIPE_TEST_KEY}" in (tmp_path / "billing-e2e.yml").read_text()
    assert '"{monthly_price}"' in (tmp_path / "scenarios" / "renewal.yml").read_text()
    (tmp_path / "billing-e2e.yml").write_text("mine")
    assert exit_code(["init"]) == 0
    assert (tmp_path / "billing-e2e.yml").read_text() == "mine"
    assert "kept billing-e2e.yml" in capsys.readouterr().out


def test_init_copies_every_builtin_scenario_and_they_list(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    exit_code(["init"])
    assert sorted(p.name for p in (tmp_path / "scenarios").iterdir()) == sorted(
        f.name for f in cli.BUILTIN.iterdir() if f.name.endswith(".yml")
    )
    assert "vars:" in (tmp_path / "billing-e2e.yml").read_text()
    assert exit_code(["list"]) == 0
    out = capsys.readouterr().out
    assert "Renewal  (6 steps)" in out
    assert "Failed renewal  (10 steps)" in out


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_list_reports_a_bad_scenario_with_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "scenarios" / "typo.yml", "steps:\n  - advanse: 1 month\n")
    assert exit_code(["list"]) == 2
    assert "did you mean advance?" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("key", "error"),
    [("sk_live_51Abc", "live key"), ("pk_test_51Abc", "publishable key")],
)
def test_run_refuses_unsafe_keys_before_calling_stripe(tmp_path, monkeypatch, capsys, key, error):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEY", key)
    write(tmp_path / "billing-e2e.yml", "stripe: {key: '${KEY}'}\n")
    write(tmp_path / "scenarios" / "a.yml", "steps:\n  - card: ok\n")
    monkeypatch.setattr(cli.stripe, "StripeClient", lambda *a, **k: pytest.fail("Stripe was called"))
    assert exit_code(["run"]) == 2
    assert error in capsys.readouterr().err


def test_run_checks_every_scenario_before_touching_stripe(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KEY", "sk_test_51Abc")
    write(tmp_path / "billing-e2e.yml", "stripe: {key: '${KEY}'}\n")
    write(tmp_path / "scenarios" / "a.yml", "steps:\n  - card: ok\n")
    write(tmp_path / "scenarios" / "b.yml", "steps:\n  - card: visa\n")
    monkeypatch.setattr(cli.stripe, "StripeClient", lambda *a, **k: pytest.fail("Stripe was called"))
    assert exit_code(["run"]) == 2
    assert "b.yml, step 1 (card): unknown card 'visa'" in capsys.readouterr().err


def test_missing_files_are_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert exit_code(["run"]) == 2
    assert "billing-e2e.yml: file not found" in capsys.readouterr().err
    write(tmp_path / "billing-e2e.yml", "stripe: {key: sk_test_1}\n")
    assert exit_code(["list", "nowhere"]) == 2
    assert "nowhere: no such file or directory" in capsys.readouterr().err


def results() -> list[RunResult]:
    ok = RunResult("Renewal", [StepResult("card ok", True, "set", 1.0, CLOCK)])
    failed = RunResult(
        "Failed renewal",
        [
            StepResult("card ok", True, "set", 1.0, CLOCK),
            StepResult("subscription status = past_due", False, "status is 'active', expected 'past_due'", 10.2, CLOCK),
        ],
        not_run=2,
    )
    broken = RunResult("Setup", errors=["setup failed: `make user` exited with 1"], not_run=3)
    return [ok, failed, broken]


def test_the_console_shows_each_step_the_reason_of_a_failure_and_a_summary():
    out = io.StringIO()
    console = Console(out)
    for result in results():
        console.scenario(result.scenario)
        for step in result.steps:
            console.step(step)
        console.outcome(result)
    console.summary(results())
    text = out.getvalue()
    assert "  ✓ card ok  (1.0 s, clock 2026-11-17)" in text
    assert "  ✗ subscription status = past_due  (10.2 s, clock 2026-11-17)\n      status is 'active'" in text
    assert "2 step(s) not run" in text
    assert "✗ setup failed" in text
    assert "2 of 3 scenario(s) failed: Failed renewal, Setup" in text
    assert "\033[" not in text  # no colors when not a terminal


def test_the_junit_report_has_one_case_per_scenario(tmp_path):
    path = tmp_path / "report.xml"
    write_junit(results(), path)
    suite = ET.parse(path).getroot()
    assert (suite.get("tests"), suite.get("failures")) == ("3", "2")
    ok, failed, broken = suite.findall("testcase")
    assert ok.find("failure") is None
    failure = failed.find("failure")
    assert failure is not None
    assert failure.get("message") == "subscription status = past_due: status is 'active', expected 'past_due'"
    assert "FAIL subscription status = past_due (10.2 s) [clock 2026-11-17 00:00]" in (
        failed.findtext("system-out") or ""
    )
    broken_failure = broken.find("failure")
    assert broken_failure is not None
    assert broken_failure.get("message") == "setup failed: `make user` exited with 1"
