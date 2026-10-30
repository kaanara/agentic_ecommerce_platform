"""WhatsApp Cloud API (Meta) channel: inbound via a push webhook, replies via a plain HTTP POST through `net_guard`.

Unlike Telegram (`getUpdates` long polling), WhatsApp pushes updates to `POST /webhooks/whatsapp` in
`aecom.api.webhooks`, which verifies `X-Hub-Signature-256` and dedupes by message id before calling
`channels.router.handle_inbound` here (see that module's docstring for the handshake / signature / idempotency
contract). This module only holds the `Channel` implementation: parsing the Graph API's webhook JSON into
`Inbound`s, and sending replies back out via the Graph API's `/messages` endpoint.

Config: `WHATSAPP_ENABLED` (`whatsapp_enabled` in Settings) turns the channel on; four secrets must all be present
(Settings > Channels): `WHATSAPP_TOKEN` (the permanent/system-user access token), `WHATSAPP_PHONE_ID` (the Cloud
API phone number id replies are sent from), `WHATSAPP_APP_SECRET` (signs inbound webhooks) and
`WHATSAPP_VERIFY_TOKEN` (the handshake secret chosen when the webhook URL is registered with Meta). The channel is
fully disabled - `enabled()` False, both webhook routes 404 - unless every one of those is set, since a half
configured channel (e.g. a verify token but no app secret) would accept unsigned traffic.

Replies go through a hand-rolled POST that reuses `net_guard.resolve_public`'s SSRF checks and IP-pinning (same
anti DNS-rebinding dial as `net_guard.pinned_get`, just for POST, which `pinned_get` does not support and
Telegram's GET-only Bot API never needed - see `telegram.py`'s docstring).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from aecom.channels.base import Button, Inbound
from aecom.config import get_settings
from aecom.net_guard import Target, resolve_public
from aecom.observability import record_error
from aecom.secrets import secrets

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
GRAPH_API_VERSION = "v21.0"
HTTP_TIMEOUT_S = 15
MAX_INTERACTIVE_BUTTONS = 3  # the Graph API refuses a reply-button message with more than 3 buttons
REQUIRED_SECRETS = ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN")


def verify_signature(raw: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Meta's webhook signature: `X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the raw body under the app
    secret>` (https://developers.facebook.com/docs/graph-api/webhooks/getting-started#verification-requests)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    provided = signature_header.removeprefix("sha256=")
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def _message_body(to: str, text: str, buttons: list[Button] | None) -> dict[str, Any]:
    """`text` for a plain reply, or an interactive reply-button message (used for approve/reject) when `buttons`
    is given; the Graph API caps button titles at 20 characters and a message at 3 buttons."""
    if buttons:
        reply_buttons = [{"type": "reply", "reply": {"id": b.data[:256], "title": b.label[:20]}}
                         for b in buttons[:MAX_INTERACTIVE_BUTTONS]]
        return {"messaging_product": "whatsapp", "to": to, "type": "interactive",
               "interactive": {"type": "button", "body": {"text": text[:1024]},
                               "action": {"buttons": reply_buttons}}}
    return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}}


def _pinned_post(target: Target, json_body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    """POST `target` without following redirects, dialling the validated `target.ip` only - the POST counterpart
    of `net_guard.pinned_get` (see this module's docstring for why it lives here instead)."""
    with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=False, trust_env=False) as client:
        request = client.build_request("POST", target.pinned_url, json=json_body,
                                       headers={**headers, "Host": target.host_header},
                                       extensions={"sni_hostname": target.ascii_host})
        return client.send(request)


def _parse_message(value: dict[str, Any], message: dict[str, Any]) -> Inbound | None:
    """One entry of `value["messages"]` -> an `Inbound`, or None for message types this channel does not handle
    (images, locations, reactions, ...; only plain text and interactive button taps are supported)."""
    sender = message.get("from")
    if not sender:
        return None
    message_id = str(message.get("id", ""))
    msg_type = message.get("type")
    if msg_type == "text":
        return Inbound(channel="whatsapp", external_chat_id=str(sender), text=str(message.get("text", {}).get("body", "")),
                       message_id=message_id)
    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        if reply:
            return Inbound(channel="whatsapp", external_chat_id=str(sender), text="", message_id=message_id,
                           callback_data=str(reply.get("id", "")))
    if msg_type == "button":  # a tap on a template's quick-reply button
        return Inbound(channel="whatsapp", external_chat_id=str(sender), text="", message_id=message_id,
                       callback_data=str(message.get("button", {}).get("payload", "")))
    return None


def iter_inbound(payload: dict[str, Any]) -> list[Inbound]:
    """Every message in a webhook delivery as an `Inbound`, walking `entry[].changes[].value.messages[]` (a single
    POST can batch several, from one sender or several); `value.statuses` (delivery/read receipts) are ignored."""
    out: list[Inbound] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for message in value.get("messages", []) or []:
                if (inbound := _parse_message(value, message)) is not None:
                    out.append(inbound)
    return out


class WhatsAppChannel:
    name = "whatsapp"

    def enabled(self) -> bool:
        return get_settings().whatsapp_enabled and all(secrets.has(name) for name in REQUIRED_SECRETS)

    def send(self, external_chat_id: str, text: str, buttons: list[Button] | None = None) -> bool:
        """POST `/{phone_id}/messages`. Returns True once the Graph API accepted it."""
        token = secrets.get("WHATSAPP_TOKEN")
        phone_id = secrets.get("WHATSAPP_PHONE_ID")
        if not token or not phone_id:
            return False
        url = f"{GRAPH_BASE}/{GRAPH_API_VERSION}/{phone_id}/messages"
        body = _message_body(external_chat_id, text, buttons)
        try:
            target = resolve_public(url)
            response = _pinned_post(target, body, {"Authorization": f"Bearer {token}"})
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - a Graph API outage must never crash the caller
            log.warning("whatsapp sendMessage failed: %s: %s", type(exc).__name__, exc)
            record_error("whatsapp", exc, context={"method": "sendMessage"})
            return False
        return True

    def parse_inbound(self, update: dict) -> Inbound | None:
        """First inbound message in a webhook delivery, to satisfy the `Channel` protocol's single-message shape;
        `aecom.api.webhooks` uses `iter_inbound` directly so a batched delivery is not truncated to one message."""
        messages = iter_inbound(update)
        return messages[0] if messages else None


CHANNEL = WhatsAppChannel()  # the module's singleton `Channel`; webhooks.py and (if wired) notify.py use this


def send(external_chat_id: str, text: str) -> bool:
    """Module-level convenience wrapper over `CHANNEL.send`, matching `telegram.send`'s `(chat_id, text) -> bool`
    shape for callers (e.g. notify.py) that don't need buttons."""
    return CHANNEL.send(external_chat_id, text)
