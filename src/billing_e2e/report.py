"""Shows the result in the terminal and writes a JUnit report for CI."""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TextIO

from billing_e2e.runner import RunResult, StepResult


class Console:
    """Live output: one line per step as it finishes, the reason under a failure, a summary at the end."""

    def __init__(self, out: TextIO = sys.stdout):
        self.out = out
        self.color = out.isatty() and "NO_COLOR" not in os.environ

    def _paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def scenario(self, name: str) -> None:
        print(f"\n{self._paint(name, '1')}", file=self.out)

    def step(self, step: StepResult) -> None:
        mark = self._paint("✓", "32") if step.passed else self._paint("✗", "31")
        clock = f", clock {step.simulated:%Y-%m-%d}" if step.simulated else ""
        print(f"  {mark} {step.label}  {self._paint(f'({step.seconds:.1f} s{clock})', '2')}", file=self.out)
        if not step.passed:
            print(f"      {step.detail}", file=self.out)

    def outcome(self, result: RunResult) -> None:
        for error in result.errors:
            print(f"  {self._paint('✗', '31')} {error}", file=self.out)
        if result.not_run:
            print(f"  {self._paint(f'{result.not_run} step(s) not run', '2')}", file=self.out)
        if result.kept:
            ids = ", ".join(f"{name} {value}" for name, value in result.kept.items())
            print(f"  kept for inspection: {ids}", file=self.out)
            customer = result.kept["customer"]
            print(f"  {self._paint(f'https://dashboard.stripe.com/test/customers/{customer}', '2')}", file=self.out)

    def summary(self, results: list[RunResult]) -> None:
        failed = [r.scenario for r in results if not r.passed]
        if failed:
            line = self._paint(f"{len(failed)} of {len(results)} scenario(s) failed: {', '.join(failed)}", "31")
        else:
            line = self._paint(f"{len(results)} scenario(s) passed", "32")
        print(f"\n{line}", file=self.out)


def step_line(step: StepResult) -> str:
    clock = f" [clock {step.simulated:%Y-%m-%d %H:%M}]" if step.simulated else ""
    return f"{'PASS' if step.passed else 'FAIL'} {step.label} ({step.seconds:.1f} s){clock}: {step.detail}"


def write_junit(results: list[RunResult], path: str | Path) -> None:
    """One test case per scenario; a failure carries the step that failed, the output lists every step."""
    suite = ET.Element(
        "testsuite",
        name="billing-e2e",
        tests=str(len(results)),
        failures=str(sum(not r.passed for r in results)),
        time=f"{sum(s.seconds for r in results for s in r.steps):.1f}",
    )
    for result in results:
        case = ET.SubElement(
            suite,
            "testcase",
            classname="billing-e2e",
            name=result.scenario,
            time=f"{sum(s.seconds for s in result.steps):.1f}",
        )
        if not result.passed:
            failed = next((s for s in result.steps if not s.passed), None)
            message = f"{failed.label}: {failed.detail}" if failed else "; ".join(result.errors)
            ET.SubElement(case, "failure", message=message).text = "\n".join(result.errors) or None
        ET.SubElement(case, "system-out").text = "\n".join(step_line(s) for s in result.steps)
    ET.indent(suite)
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
