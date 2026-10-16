"""Refuses live keys and checks the account is in test mode; keeps keys out of every output."""

import re

import stripe

# Secret keys, restricted keys and webhook signing secrets, in either mode: only the prefix is kept
SECRET = re.compile(r"\b((?:sk|rk)_(?:test|live)_|whsec_)[A-Za-z0-9]+")
TEST_KEY = re.compile(r"(?:sk|rk)_test_[A-Za-z0-9]+")


class UnsafeKeyError(Exception):
    """The key cannot be used: it is live, malformed, or Stripe does not accept it as a test key."""


def redact(text: str) -> str:
    """Hide every Stripe secret in `text`, keeping its type (sk_test_…) so the message stays readable."""
    return SECRET.sub(r"\1…", text)


def check_key(key: str) -> None:
    """Accept only a secret or restricted *test* key (sk_test_…, rk_test_…). Nothing is sent to Stripe."""
    if key.startswith(("sk_live_", "rk_live_")):
        raise UnsafeKeyError("This is a live key. billing-e2e only runs in test mode: use a sk_test_ or rk_test_ key.")
    if key.startswith(("pk_test_", "pk_live_")):
        raise UnsafeKeyError("This is a publishable key: use a secret (sk_test_) or restricted (rk_test_) test key.")
    if not TEST_KEY.fullmatch(key):
        raise UnsafeKeyError("This does not look like a Stripe test key (expected sk_test_… or rk_test_…).")


def confirm_test_mode(client: stripe.StripeClient) -> None:
    """Ask Stripe itself. Test clocks exist only in test mode, so listing them proves the key is a working test key,
    and that it has the test clock permission every run needs."""
    try:
        client.v1.test_helpers.test_clocks.list({"limit": 1})
    except stripe.AuthenticationError as exc:
        raise UnsafeKeyError("Stripe rejected this key (revoked or mistyped).") from exc
    except stripe.PermissionError as exc:
        raise UnsafeKeyError("This restricted key cannot manage test clocks: give it write access to them.") from exc
    except stripe.StripeError as exc:
        raise UnsafeKeyError(f"Stripe refused the test mode check: {redact(str(exc))}") from exc
