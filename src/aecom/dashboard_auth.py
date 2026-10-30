"""Password gate for the Streamlit dashboard (pure functions; the Streamlit glue lives in dashboard/_common.py).

The login is managed from the dashboard (Settings > Security): the mode (``dashboard_auth_mode`` on/off) and the hash
(``dashboard_password_hash``, "scrypt$<salt_hex>$<hash_hex>") live in ops_settings and win over .env. Without a database
value the old .env variables still apply: DASHBOARD_PASSWORD_HASH (generate it with
`uv run python -m aecom.dashboard_auth`) and DASHBOARD_AUTH=off. No hash at all = fail closed.
Only the functions in this module write these two keys; no agent tool can reach them.
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
MODE_KEY = "dashboard_auth_mode"
HASH_KEY = "dashboard_password_hash"
MIN_PASSWORD_LENGTH = 12
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32
MAX_FAILURES = 5
LOCKOUT_SECONDS = 60.0


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def _db_get(key: str) -> str | None:
    """The stored value, or None (also when the database is not reachable yet: callers then fall back to .env)."""
    try:
        from aecom.company_settings import get_setting

        return get_setting(key)
    except Exception:  # noqa: BLE001
        return None


def disabled() -> bool:
    """True only when the login is explicitly off: the database mode "off", else DASHBOARD_AUTH=off (or false/0/no)
    in .env. Anything else keeps the login on."""
    mode = (_db_get(MODE_KEY) or "").strip().lower()
    if mode in {"on", "off"}:
        return mode == "off"
    return (secrets.get("DASHBOARD_AUTH") or "").strip().lower() in {"off", "false", "0", "no"}


def configured_hash() -> str | None:
    return (_db_get(HASH_KEY) or "").strip() or (secrets.get(ENV_VAR) or "").strip() or None


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


def confirm_password(password: str, state: MutableMapping[str, Any], now: Callable[[], float] = time.monotonic) -> bool:
    """Re-check the current password for a sensitive action (step-up). Shares the login lockout; never sets
    ``authenticated``."""
    if locked_out(state, now):
        return False
    if verify_password(password):
        state["auth_failures"] = 0
        return True
    state["auth_failures"] = int(state.get("auth_failures", 0)) + 1
    if state["auth_failures"] >= MAX_FAILURES:
        state["auth_locked_until"] = now() + LOCKOUT_SECONDS
    return False


def touch(state: MutableMapping[str, Any], now: Callable[[], float] = time.time) -> None:
    state["last_seen"] = now()


def idle_expired(state: MutableMapping[str, Any], hours: float, now: Callable[[], float] = time.time) -> bool:
    """True when the signed-in session was idle longer than ``hours`` (0 or less = never expires)."""
    last = state.get("last_seen")
    return bool(hours > 0 and last is not None and now() - float(last) > hours * 3600)


def _put(key: str, value: str) -> None:
    from aecom.company_settings import set_setting

    set_setting(key, value)


def _record(by: str, action: str) -> None:
    from aecom.audit import audit

    audit(f"human:{by}" if by else "system", f"dashboard_login_{action}", "ok", {})
    try:
        from aecom import notify

        notify.route({"kind": "settings", "text": f"Dashboard login: {action.replace('_', ' ')} (by {by or 'system'})",
                      "urgency": "info"})
    except Exception:  # noqa: BLE001 - a notification channel being down must not block the change
        pass


def _enforced() -> bool:
    return not disabled() and configured_hash() is not None


def set_password(new: str, repeat: str, by: str, current: str = "", state: MutableMapping[str, Any] | None = None) -> None:
    """Store a new password hash and turn the login on. While the login is enforced the current password is required.
    Raises ValueError with a message fit for the page."""
    if not by.strip():
        raise ValueError("enter your name so the change is recorded")
    if len(new) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    if new != repeat:
        raise ValueError("the two passwords do not match")
    if _enforced() and not confirm_password(current, state if state is not None else {}):
        raise ValueError("the current password is wrong (or too many tries: wait a minute)")
    _put(HASH_KEY, hash_password(new))
    _put(MODE_KEY, "on")
    _record(by, "password_set")


def turn_login_on(by: str) -> None:
    if configured_hash() is None:
        raise ValueError("set a password first")
    _put(MODE_KEY, "on")
    _record(by, "on")


def turn_login_off(by: str, current: str = "", state: MutableMapping[str, Any] | None = None) -> None:
    """Switch the login off. If a password exists the current one is required."""
    if configured_hash() is not None and not confirm_password(current, state if state is not None else {}):
        raise ValueError("the current password is wrong (or too many tries: wait a minute)")
    _put(MODE_KEY, "off")
    _record(by, "off")


def reset_password_local(new: str, by: str = "shell") -> None:
    """Recovery from a shell on the machine (`aecom dashboard reset-password`): no current password needed."""
    if len(new) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"the password needs at least {MIN_PASSWORD_LENGTH} characters")
    _put(HASH_KEY, hash_password(new))
    _put(MODE_KEY, "on")
    _record(by, "password_reset")


def main() -> None:
    import getpass

    password = getpass.getpass("New dashboard password: ")
    if not password or password != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords empty or not matching; nothing printed.")
    print("Add this line to .env:")
    print(f"{ENV_VAR}={hash_password(password)}")


if __name__ == "__main__":
    main()
