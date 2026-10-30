"""Telegram Bot API through ``getUpdates`` long polling (plain HTTP, GET only, through ``net_guard``).

Telegram's Bot API accepts every method as a GET with query-string parameters, so this never needs ``net_guard``
to support POST: ``pinned_get`` is enough and reuses the existing SSRF guard unchanged. ``poll_updates`` is a
one-shot call (meant to be a short-interval scheduler job, see the patch snippet in builder E's final report),
not a blocking long-poll loop: each call fetches whatever is waiting with a short server-side ``timeout``.

Config: ``TELEGRAM_ENABLED`` (``telegram_enabled`` in Settings) turns the channel on; the token is the secret
``TELEGRAM_BOT_TOKEN``. Never raises: every failure is logged and recorded via ``observability.record_error``.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from aecom import company_settings
from aecom.channels.base import Button, Inbound
from aecom.channels.router import handle_inbound
from aecom.config import get_settings
from aecom.net_guard import pinned_get, resolve_public
from aecom.observability import record_error
from aecom.secrets import secrets

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
OFFSET_KEY = "telegram_update_offset"
POLL_TIMEOUT_S = 0  # one-shot per scheduler tick, not a blocking long-poll (see module docstring)
HTTP_TIMEOUT_S = 15


def _token() -> str | None:
    return secrets.get("TELEGRAM_BOT_TOKEN")


def _call(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """One Telegram Bot API call via ``net_guard``. Returns the parsed ``result``, or None on any failure."""
    token = _token()
    if not token:
        return None
    url = f"{API_BASE}/bot{token}/{method}?{urlencode(params)}"
    try:
        target = resolve_public(url)
        response = pinned_get(target, timeout=HTTP_TIMEOUT_S)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - a Telegram outage must never stop the scheduler
        log.warning("telegram %s failed: %s: %s", method, type(exc).__name__, exc)
        record_error("telegram", exc, context={"method": method})
        return None
    if not body.get("ok"):
        log.warning("telegram %s rejected: %s", method, body.get("description"))
        return None
    return body.get("result")


def _reply_markup(buttons: list[Button] | None) -> str | None:
    if not buttons:
        return None
    return json.dumps({"inline_keyboard": [[{"text": b.label, "callback_data": b.data}] for b in buttons]})


class TelegramChannel:
    name = "telegram"

    def enabled(self) -> bool:
        return get_settings().telegram_enabled and secrets.has("TELEGRAM_BOT_TOKEN")

    def send(self, external_chat_id: str, text: str, buttons: list[Button] | None = None) -> bool:
        """``sendMessage``. Returns True once Telegram accepted it."""
        params: dict[str, Any] = {"chat_id": external_chat_id, "text": text[:4096]}
        if markup := _reply_markup(buttons):
            params["reply_markup"] = markup
        return _call("sendMessage", params) is not None

    def answer_callback(self, callback_id: str) -> bool:
        """Acknowledge a button tap so Telegram stops showing its loading spinner."""
        if not callback_id:
            return False
        return _call("answerCallbackQuery", {"callback_query_id": callback_id}) is not None

    def parse_inbound(self, update: dict) -> Inbound | None:
        if cq := update.get("callback_query"):
            msg = cq.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id is None:
                return None
            return Inbound(channel=self.name, external_chat_id=str(chat_id), text="",
                           message_id=str(msg.get("message_id", "")), callback_data=str(cq.get("data", "")),
                           callback_id=str(cq.get("id", "")))
        message = update.get("message")
        if not message:
            return None
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return None
        return Inbound(channel=self.name, external_chat_id=str(chat_id), text=str(message.get("text", "")),
                       message_id=str(message.get("message_id", "")))


CHANNEL = TelegramChannel()  # the module's singleton `Channel`; notify.py looks this up for its owner-message path


def send(external_chat_id: str, text: str) -> bool:
    """Module-level convenience wrapper over ``CHANNEL.send``, for callers (e.g. notify.py) that just want a
    ``(chat_id, text) -> bool`` function and don't need buttons."""
    return CHANNEL.send(external_chat_id, text)


def poll_updates() -> int:
    """Fetch waiting updates once, route each through the channel router, send the replies back. Never raises.

    Returns how many updates were processed (0 if the channel is off, misconfigured, or Telegram could not be
    reached - all of those are logged, not raised, so a scheduler job calling this on a tick never dies from it).
    """
    channel = TelegramChannel()
    if not channel.enabled():
        return 0
    offset_raw = company_settings.get_setting(OFFSET_KEY)
    offset = int(offset_raw) + 1 if offset_raw else None
    params: dict[str, Any] = {"timeout": POLL_TIMEOUT_S,
                              "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset is not None:
        params["offset"] = offset
    updates = _call("getUpdates", params)
    if not updates:
        return 0
    processed = 0
    for update in updates:
        company_settings.set_setting(OFFSET_KEY, str(update["update_id"]))
        msg = channel.parse_inbound(update)
        if msg is None:
            continue
        try:
            replies = handle_inbound(msg)
        except Exception as exc:  # noqa: BLE001 - one bad update must not stop the rest of the batch
            log.warning("telegram update %s failed: %s", update.get("update_id"), exc)
            record_error("telegram", exc, context={"update_id": update.get("update_id")})
            replies = []
        if msg.callback_id:
            channel.answer_callback(msg.callback_id)
        for reply in replies:
            channel.send(msg.external_chat_id, reply.text, reply.buttons)
        processed += 1
    return processed
