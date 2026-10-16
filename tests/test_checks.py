import pytest

from billing_e2e.checks import wait_until


def test_wait_until_returns_the_first_truthy_result():
    answers = iter([None, 0, "ready"])
    assert wait_until(lambda: next(answers), what="x", timeout=5, interval=0) == "ready"


def test_wait_until_gives_up_with_a_clear_message():
    with pytest.raises(TimeoutError, match="Gave up waiting for the clock after 0 s"):
        wait_until(lambda: False, what="the clock", timeout=0, interval=0)
