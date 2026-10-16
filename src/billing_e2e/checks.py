"""Checks on the application under test (an HTTP call or a command), and waiting until a condition holds."""

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from billing_e2e.safety import redact

T = TypeVar("T")

# The application handles Stripe's webhooks a moment after the event: a check is retried until then
CHECK_TIMEOUT = 30
MISSING = object()


@dataclass
class Result:
    passed: bool
    detail: str  # what was seen, shown when the check fails


class CheckFailed(AssertionError):
    pass


def wait_until(condition: Callable[[], T | None], *, what: str, timeout: float, interval: float = 1.0) -> T:
    """Call `condition` until it returns something truthy, and return that. Waits on a state, never a fixed delay."""
    deadline = time.monotonic() + timeout
    while True:
        result = condition()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Gave up waiting for {what} after {timeout:g} s")
        time.sleep(interval)


def eventually(check: Callable[[], Result], *, what: str, timeout: float = CHECK_TIMEOUT) -> Result:
    """Retry `check` until it passes. On timeout, fail with what the application last answered."""
    last = Result(False, "not run")

    def passed() -> Result | None:
        nonlocal last
        last = check()
        return last if last.passed else None

    try:
        return wait_until(passed, what=what, timeout=timeout)
    except TimeoutError:
        raise CheckFailed(f"{what}: still failing after {timeout:g} s. {last.detail}") from None


def lookup(data: Any, path: str) -> Any:
    """Read a dotted path in decoded JSON: "plan.name", "credits.0.remaining". Returns MISSING if absent."""
    for part in path.split("."):
        if isinstance(data, Mapping) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.lstrip("-").isdigit() and -len(data) <= int(part) < len(data):
            data = data[int(part)]
        else:
            return MISSING
    return data


def http_check(
    client: httpx.Client, method: str, url: str, expect: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> Result:
    """Call the application's API and compare fields of its JSON answer: {"plan.name": "Business"}."""
    try:
        response = client.request(method, url, headers=dict(headers or {}))
    except httpx.HTTPError as exc:
        return Result(False, f"{method} {url} failed: {exc.__class__.__name__}: {redact(str(exc))}")
    if response.status_code >= 400:
        return Result(False, f"{method} {url} answered {response.status_code}: {redact(response.text[:300])}")
    try:
        data = response.json()
    except json.JSONDecodeError:
        return Result(False, f"{method} {url} did not answer JSON: {redact(response.text[:300])}")
    wrong = []
    for path, expected in expect.items():
        actual = lookup(data, path)
        if actual is MISSING:
            wrong.append(f"{path} is missing")
        elif actual != expected:
            wrong.append(f"{path} is {actual!r}, expected {expected!r}")
    return Result(not wrong, "; ".join(wrong) or "all fields match")


def command_check(command: str, env: Mapping[str, str] | None = None, timeout: float = 60) -> Result:
    """Run a command of the application (any language): the check passes when it exits with 0.

    `env` is added to the environment, e.g. BILLING_E2E_CUSTOMER, so the command knows which run to look at.
    """
    try:
        done = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})}
        )
    except subprocess.TimeoutExpired:
        return Result(False, f"`{command}` did not finish within {timeout:g} s")
    output = redact((done.stdout + done.stderr).strip()[-500:])
    if done.returncode:
        return Result(False, f"`{command}` exited with {done.returncode}" + (f": {output}" if output else ""))
    return Result(True, output or "exited with 0")
