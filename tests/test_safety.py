from types import SimpleNamespace
from typing import Any

import pytest
import stripe

from billing_e2e.safety import UnsafeKeyError, check_key, confirm_test_mode, redact


@pytest.mark.parametrize("key", ["sk_test_51Abc123", "rk_test_51Abc123"])
def test_test_keys_are_accepted(key):
    check_key(key)


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("sk_live_51Abc123", "live key"),
        ("rk_live_51Abc123", "live key"),
        ("pk_test_51Abc123", "publishable"),
        ("pk_live_51Abc123", "publishable"),
        ("", "does not look like"),
        ("sk_test_", "does not look like"),
        ("sk_test_51Abc 123", "does not look like"),
        (" sk_test_51Abc123", "does not look like"),
    ],
)
def test_other_keys_are_refused(key, reason):
    with pytest.raises(UnsafeKeyError, match=reason):
        check_key(key)


def test_redact_hides_every_secret_but_keeps_its_type():
    text = "keys sk_live_51Abc, rk_test_9Z and whsec_xyz; ids cus_123 and sub_456 stay"
    assert redact(text) == "keys sk_live_…, rk_test_… and whsec_…; ids cus_123 and sub_456 stay"


def client_raising(error: Exception | None) -> Any:
    def list_clocks(_params):
        if error:
            raise error

    test_clocks = SimpleNamespace(list=list_clocks)
    return SimpleNamespace(v1=SimpleNamespace(test_helpers=SimpleNamespace(test_clocks=test_clocks)))


def test_a_working_test_key_passes_the_stripe_check():
    confirm_test_mode(client_raising(None))


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (stripe.AuthenticationError("Invalid API Key provided: sk_test_****abcd"), "rejected"),
        (stripe.PermissionError("The provided key does not have the required permissions"), "write access"),
        (stripe.InvalidRequestError("Test clocks are only available in test mode sk_live_51Abc", None), "refused"),
    ],
)
def test_stripe_refusals_become_clear_errors_without_secrets(error, reason):
    with pytest.raises(UnsafeKeyError, match=reason) as refused:
        confirm_test_mode(client_raising(error))
    assert "sk_live_51Abc" not in str(refused.value)
