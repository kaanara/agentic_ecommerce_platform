"""Owner notifications: one router for everything that should reach the owner.

``route(event)`` delivers an event on the best available channel: a paired Telegram chat first, then ntfy, otherwise it
only appears in the dashboard inbox. Event keys: ``kind`` (``message`` | ``approval`` | ``incident`` | ``diagnosis`` |
``flash`` | ``settings``), ``text``, ``urgency`` (``info`` | ``normal`` | ``high``; high ignores quiet hours),
optional ``conversation_id``, ``approval_id``, ``agent_id``. Returns ``{channel_name: delivered}``.

Telegram sending goes through ``aecom.channels.telegram`` when that module provides a sender (lazy import, so this
module never hard-depends on it); until then (or if it fails) a pluggable hook registered with
``set_telegram_sender`` is tried. Both are optional - a missing/failed sender just means ``telegram`` is False below.
Network errors never raise: a channel that is down is simply not delivered.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

import httpx
from sqlalchemy import select

from aecom.config import get_settings
from aecom.db import ChannelLink, session
from aecom.observability import mask, record_error
from aecom.secrets import secrets

log = logging.getLogger(__name__)

TIMEOUT = 10

# Pluggable fallback used when aecom.channels.telegram has no usable sender yet (builder E wires the real one).
_telegram_sender: Callable[[str, str], bool] | None = None


def set_telegram_sender(fn: Callable[[str, str], bool] | None) -> None:
    """Register a fallback ``(chat_id, text) -> bool`` sender, used only if ``channels.telegram`` has none."""
    global _telegram_sender
    _telegram_sender = fn


def _failed(channel: str, exc: BaseException, **context: Any) -> None:
    detail = mask(str(exc))
    log.warning("%s notify channel failed: %s: %s", channel, type(exc).__name__, detail)
    record_error("notify", RuntimeError(f"{channel} failed: {type(exc).__name__}: {detail}"),
                 context={"channel": channel, **context})


def _in_quiet_hours() -> bool:
    s = get_settings()
    start, end = (s.quiet_hours_start or "").strip(), (s.quiet_hours_end or "").strip()
    if not start or not end:
        return False
    try:
        start_t = datetime.strptime(start, "%H:%M").time()
        end_t = datetime.strptime(end, "%H:%M").time()
    except ValueError:
        return False
    now_t = datetime.now().time()
    if start_t <= end_t:
        return start_t <= now_t < end_t
    return now_t >= start_t or now_t < end_t  # wraps past midnight


def _paired_telegram_chats() -> list[str]:
    with session() as s:
        return list(s.scalars(select(ChannelLink.external_chat_id)
                              .where(ChannelLink.channel == "telegram", ChannelLink.active.is_(True))))


def _telegram_module_sender() -> Callable[[str, str], bool] | None:
    """``channels.telegram``'s own sender, if that module exists and offers one. Never raises on import."""
    try:
        from aecom.channels import telegram as telegram_module
    except ImportError:
        return None
    sender = getattr(telegram_module, "send", None)
    if sender is None:
        channel_obj = getattr(telegram_module, "channel", None) or getattr(telegram_module, "CHANNEL", None)
        sender = getattr(channel_obj, "send", None)
    return sender


def _send_telegram(text: str) -> bool:
    chat_ids = _paired_telegram_chats()
    if not chat_ids:
        return False
    senders = [s for s in (_telegram_module_sender(), _telegram_sender) if s is not None]
    delivered = False
    for chat_id in chat_ids:
        for sender in senders:
            try:
                if sender(chat_id, text):
                    delivered = True
                    break
            except Exception as exc:  # noqa: BLE001 - one bad sender must not block the others or raise
                _failed("telegram", exc, chat_id=chat_id)
    return delivered


def _send_ntfy(text: str, urgency: str) -> bool:
    url = secrets.get("NTFY_URL")
    if not url:
        return False
    headers = {"Title": "aecom".encode("ascii", "replace").decode(),
              "Priority": "high" if urgency == "high" else "default"}
    if token := secrets.get("NTFY_TOKEN"):
        headers["Authorization"] = "******"
    try:
        r = httpx.post(url, content=mask(text).encode(), headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as exc:
        _failed("ntfy", exc)
        return False


def route(event: dict[str, Any]) -> dict[str, bool]:
    """Deliver ``event`` on the best channel. Never raises; the dashboard inbox always has it regardless."""
    text = str(event.get("text") or "")
    urgency = event.get("urgency", "normal")
    result = {"dashboard": True, "telegram": False, "ntfy": False}
    if _in_quiet_hours() and urgency != "high":
        return result
    result["telegram"] = _send_telegram(text)
    if not result["telegram"]:
        result["ntfy"] = _send_ntfy(text, urgency)
    return result
