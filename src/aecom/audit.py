"""Audit trail (``ops_audit_log``). Details are masked before they are stored (I16): secrets never land in plain text."""

from __future__ import annotations

from typing import Any

from aecom.db import AuditLog, session

# Keys whose values are never stored: licence keys, download links, tokens and credentials (I16).
REDACT_KEYS = ("license_key", "download_url", "token", "api_key", "password", "secret", "authorization")


def _redacted(key: str, value: Any) -> Any:
    """Like ``catalog._redact_access``: a licence key keeps its last 4 characters, a link only that it was sent."""
    if value in (None, ""):
        return value
    if key == "license_key":
        return f"****{str(value)[-4:]}"
    if key == "download_url":
        return "(sent to the customer)"
    return "***"


def _redact_key(key: Any) -> str | None:
    k = str(key).lower()
    return next((name for name in REDACT_KEYS if k == name or k.endswith("_" + name)), None)


def mask_detail(value: Any) -> Any:
    """Recursively mask a JSON-like structure: secret-shaped strings via ``observability.mask``, values of
    sensitive keys (``REDACT_KEYS``, also as ``*_<key>`` suffix such as ``access_token``) redacted."""
    from aecom.observability import mask

    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            name = _redact_key(k)
            out[k] = _redacted(name, v) if name else mask_detail(v)
        return out
    if isinstance(value, (list, tuple, set)):
        return [mask_detail(v) for v in value]
    return value


def audit(actor: str, action: str, decision: str, detail: dict[str, Any] | None = None, run_id: str | None = None) -> None:
    with session() as s:
        s.add(AuditLog(actor=actor, action=action, decision=decision, detail=mask_detail(detail or {}), run_id=run_id))
