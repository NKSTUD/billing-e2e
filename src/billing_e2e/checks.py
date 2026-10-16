"""Checks on the application under test (an HTTP call or a command), and waiting until a condition holds."""

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


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
