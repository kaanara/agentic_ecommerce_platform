"""Error tracking (K1): unexpected exceptions land in ``ops_errors`` with a full, secret-masked traceback.

Optionally forwarded to Sentry when ``SENTRY_DSN`` is set and ``sentry-sdk`` is installed
(``uv sync --extra observability``). Everything here is best effort: recording an error must never
raise, otherwise error handling itself becomes a new failure mode.
"""

from __future__ import annotations

import json
import logging
import os
import re
import traceback as tb
from typing import Any

log = logging.getLogger(__name__)

MASK = "***"
_PATTERNS = [
    re.compile(r"am_us_[A-Za-z0-9_]+"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"),
]
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_DSN")
_MIN_SECRET_LEN = 6  # don't mask trivial values like "1" or "true" everywhere

_sentry_ready: bool | None = None  # None = not tried yet


def _secret_values() -> list[str]:
    from aecom.secrets import secrets

    names = set(os.environ) | set(getattr(secrets, "_dotenv", {}))
    values = {v for n in names if n.upper().endswith(_SECRET_SUFFIXES)
              for v in [secrets.get(n)] if v and len(v) >= _MIN_SECRET_LEN}
    return sorted(values, key=len, reverse=True)  # longest first, so a value containing another is fully masked


def mask(text: str) -> str:
    """Replace known secret shapes and the values of *_KEY/*_TOKEN/*_SECRET/*_DSN env vars with ***."""
    for value in _secret_values():
        text = text.replace(value, MASK)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + MASK, text)
    return text


def _mask_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    return json.loads(mask(json.dumps(context, default=str)))


def _mask_any(value: Any) -> Any:
    """Recursively mask every string inside a (JSON-like) structure, e.g. a Sentry event."""
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, dict):
        return {k: _mask_any(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_mask_any(v) for v in value)
    return value


def _sentry_before_send(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Sentry hook: mask exception values, frame source lines, messages, breadcrumbs and extra before upload.

    If masking itself fails the event is dropped: better no Sentry event than an unmasked one.
    """
    try:
        return _mask_any(event)
    except Exception:
        log.warning("could not mask sentry event; dropping it", exc_info=True)
        return None


def _sentry_before_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    try:
        return _mask_any(crumb)
    except Exception:
        return None


def _sentry_capture(exc: BaseException) -> str | None:
    global _sentry_ready
    from aecom.secrets import secrets

    dsn = secrets.get("SENTRY_DSN")
    if not dsn:
        return None
    try:
        import sentry_sdk
    except ImportError:
        return None
    if _sentry_ready is None:
        try:
            sentry_sdk.init(dsn=dsn, send_default_pii=False, include_local_variables=False,
                            before_send=_sentry_before_send, before_breadcrumb=_sentry_before_breadcrumb)
            _sentry_ready = True
        except Exception:  # bad DSN etc.: don't retry on every error
            log.warning("sentry init failed; errors are kept in ops_errors only", exc_info=True)
            _sentry_ready = False
    if not _sentry_ready:
        return None
    return sentry_sdk.capture_exception(exc)


def record_error(component: str, exc: BaseException, *, run_id: str | None = None, event_id: int | None = None,
                 context: dict[str, Any] | None = None) -> int | None:
    """Store an unexpected error (type, message, traceback, context; secrets masked). Never raises."""
    try:
        sentry_id = None
        try:
            sentry_id = _sentry_capture(exc)
        except Exception:
            log.warning("sentry capture failed", exc_info=True)
        from aecom.db import ErrorRow, session

        trace = "".join(tb.format_exception(type(exc), exc, exc.__traceback__))
        with session() as s:
            row = ErrorRow(component=component, run_id=run_id, event_id=event_id, exc_type=type(exc).__name__,
                           message=mask(str(exc))[:4000], traceback=mask(trace)[:20000],
                           context=_mask_context(context), sentry_event_id=sentry_id)
            s.add(row)
            s.flush()
            return row.id
    except Exception:
        log.warning("could not record %s error %s", component, type(exc).__name__, exc_info=True)
        return None
