"""Password gate for the Streamlit dashboard (pure functions; the Streamlit glue lives in dashboard/_common.py).

The hash lives in DASHBOARD_PASSWORD_HASH as "scrypt$<salt_hex>$<hash_hex>". Generate it with
`uv run python -m aecom.dashboard_auth` and paste the printed line into .env. Unset = fail closed.
DASHBOARD_AUTH=off explicitly disables the login (local development only; the dashboard then shows a warning).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Callable, MutableMapping
from typing import Any

from aecom.secrets import secrets

ENV_VAR = "DASHBOARD_PASSWORD_HASH"
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60.0


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def disabled() -> bool:
    """True only when DASHBOARD_AUTH is explicitly "off" (or false/0/no). Anything else keeps the login on."""
    return (secrets.get("DASHBOARD_AUTH") or "").strip().lower() in {"off", "false", "0", "no"}


def configured_hash() -> str | None:
    return (secrets.get(ENV_VAR) or "").strip() or None


def is_configured() -> bool:
    return configured_hash() is not None


def verify_password(password: str, stored: str | None = None) -> bool:
    """True only if `password` matches the stored hash. Missing or malformed hash -> False (fail closed)."""
    stored = stored if stored is not None else configured_hash()
    if not stored or not password:
        return False
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        return False
    return hmac.compare_digest(_scrypt(password, salt), expected)


def locked_out(state: MutableMapping[str, Any], now: Callable[[], float] = time.monotonic) -> float:
    """Seconds left in the lockout (0 = may try). `state` is per session (e.g. st.session_state)."""
    until = float(state.get("auth_locked_until", 0.0))
    remaining = until - now()
    if remaining > 0:
        return remaining
    if until:
        state["auth_locked_until"] = 0.0
        state["auth_failures"] = 0
    return 0.0


def attempt_login(password: str, state: MutableMapping[str, Any], now: Callable[[], float] = time.monotonic,
                  stored: str | None = None) -> bool:
    """Check a password with a small rate limit: after MAX_FAILURES wrong tries, refuse for LOCKOUT_SECONDS."""
    if locked_out(state, now):
        return False
    if verify_password(password, stored):
        state["auth_failures"] = 0
        state["authenticated"] = True
        return True
    state["auth_failures"] = int(state.get("auth_failures", 0)) + 1
    if state["auth_failures"] >= MAX_FAILURES:
        state["auth_locked_until"] = now() + LOCKOUT_SECONDS
    return False


def main() -> None:
    import getpass

    password = getpass.getpass("New dashboard password: ")
    if not password or password != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords empty or not matching; nothing printed.")
    print("Add this line to .env:")
    print(f"{ENV_VAR}={hash_password(password)}")


if __name__ == "__main__":
    main()
