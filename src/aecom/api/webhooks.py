"""Inbound provider webhooks: signed, idempotent, fail closed.

Not behind the bearer token (providers can't send it). Instead every route checks the provider's signature over the
**raw** body before anything is parsed or written:

* ``POST /webhooks/lemonsqueezy``: ``X-Signature`` = hex HMAC-SHA256 of the body under ``LEMONSQUEEZY_WEBHOOK_SECRET``
  (https://docs.lemonsqueezy.com/help/webhooks/signing-requests).
* ``POST /webhooks/agentmail``: Svix headers ``svix-id`` / ``svix-timestamp`` / ``svix-signature``, base64
  HMAC-SHA256 of ``{id}.{timestamp}.{body}`` under the base64 part of the ``whsec_`` secret
  ``AGENTMAIL_WEBHOOK_SECRET``, 5-minute timestamp tolerance (https://docs.agentmail.to/webhook-verification,
  https://docs.svix.com/receiving/verifying-payloads/how-manual).

No secret configured -> 503; bad or missing signature -> 401; nothing is written in either case.
Each delivery is recorded once in ``WebhookReceipt`` (unique ``(provider, event_key)``) in the same transaction as
its side effects (a failure rolls back both, so the provider's retry is processed); a repeat returns
200 ``{"duplicate": true}`` without side effects. Only these routes may be reachable from the internet (via a
tunnel); the dashboard and the rest of the API must never be exposed.

* ``GET``/``POST /webhooks/whatsapp``: Meta Cloud API, see the dedicated section near the bottom of this file -
  signed differently (``X-Hub-Signature-256``) and, unlike the two above, 404 while disabled rather than 503.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from aecom import consent
from aecom.bus.events import drain, publish
from aecom.channels import whatsapp as wa
from aecom.channels.router import handle_inbound as whatsapp_handle_inbound
from aecom.db import WebhookReceipt, session
from aecom.observability import record_error
from aecom.secrets import secrets
from aecom.store import lemonsqueezy as ls

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SVIX_TOLERANCE_S = 5 * 60


def _now() -> float:
    return time.time()


def _json(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(400, "body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "body is not a JSON object")
    return payload


# --------------------------------------------------------------------------- Lemon Squeezy
def _lemonsqueezy(raw: bytes, signature: str | None, event_header: str | None) -> dict[str, Any]:
    secret = secrets.get("LEMONSQUEEZY_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "LEMONSQUEEZY_WEBHOOK_SECRET not configured")
    if not ls.verify_signature(raw, signature, secret=secret):
        raise HTTPException(401, "invalid or missing X-Signature")
    payload = _json(raw)
    meta = payload.setdefault("meta", {})
    if not isinstance(meta, dict):
        raise HTTPException(400, "meta is not an object")
    event = meta.get("event_name") or event_header or ""
    meta["event_name"] = event
    # Lemon Squeezy sends no delivery id; a retry carries the identical body, so its hash is the key
    event_key = str(meta.get("event_id") or hashlib.sha256(raw).hexdigest())
    try:
        with session() as s:  # receipt and mirror commit together: a failure records neither, so the retry is processed
            s.add(WebhookReceipt(provider=ls.PROVIDER, event_key=event_key, topic=event))
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                return {"duplicate": True, "event": event}
            result = ls.dispatch(payload, session=s)
    except Exception as exc:
        record_error("webhook.lemonsqueezy", exc, context={"event": event, "event_key": event_key})
        raise HTTPException(500, "processing failed; retry") from exc
    return {"duplicate": False, **result}


@router.post("/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request, bg: BackgroundTasks) -> dict[str, Any]:
    """Lemon Squeezy order_created / order_refunded / license_key_* -> local mirror (others acknowledged)."""
    raw = await request.body()
    result = await run_in_threadpool(_lemonsqueezy, raw, request.headers.get("x-signature"),
                                     request.headers.get("x-event-name"))
    if not result["duplicate"]:
        bg.add_task(drain)
    return result


# --------------------------------------------------------------------------- AgentMail (Svix)
def verify_svix(raw: bytes, msg_id: str | None, timestamp: str | None, signature: str | None, secret: str) -> bool:
    """Svix scheme: any ``v1,<base64>`` entry in ``svix-signature`` must equal base64 HMAC-SHA256 of
    ``{svix-id}.{svix-timestamp}.{body}``, keyed with the base64-decoded part after ``whsec_``; the timestamp must be
    within 5 minutes of now (replay protection)."""
    if not (msg_id and timestamp and signature):
        return False
    try:
        ts = int(timestamp)
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, binascii.Error):
        return False
    if abs(_now() - ts) > SVIX_TOLERANCE_S:
        return False
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + raw
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    ok = False
    for entry in signature.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(expected, sig.encode()):
            ok = True  # no early exit: compare every entry
    return ok


def _email_payload(payload: dict[str, Any]) -> dict[str, Any]:
    msg = payload.get("message", payload)
    sender = msg.get("from") or msg.get("from_", "")
    if isinstance(sender, list):
        sender = sender[0] if sender else ""
    return {"from": sender, "subject": msg.get("subject", ""),
            "body": msg.get("extracted_text") or msg.get("text") or "",
            "message_id": msg.get("message_id"), "thread_id": msg.get("thread_id"),
            "labels": msg.get("labels", [])}


# I14: delivery problems suppress the address for marketing (https://docs.agentmail.to/events)
SUPPRESSING_EVENTS = {"message.bounced": ("bounce", "bounce"), "message.complained": ("complaint", "complaint")}


def _recipients(payload: dict[str, Any], key: str) -> list[str]:
    """``bounce.recipients`` is a list of ``{address, status}``; ``complaint.recipients`` a list of strings."""
    body = payload.get(key) or {}
    out = []
    for r in body.get("recipients", []) if isinstance(body, dict) else []:
        addr = r.get("address") if isinstance(r, dict) else r
        if isinstance(addr, str) and addr.strip():
            out.append(addr)
    return out


def _agentmail(raw: bytes, msg_id: str | None, timestamp: str | None, signature: str | None) -> dict[str, Any]:
    secret = secrets.get("AGENTMAIL_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "AGENTMAIL_WEBHOOK_SECRET not configured")
    if not verify_svix(raw, msg_id, timestamp, signature, secret):
        raise HTTPException(401, "invalid, expired or missing svix signature")
    payload = _json(raw)
    event = payload.get("event_type") or "message.received"
    try:
        with session() as s:  # receipt and event commit together: a failure records neither
            receipt = WebhookReceipt(provider="agentmail", event_key=str(msg_id), topic=event)
            s.add(receipt)
            s.flush()
            if event in SUPPRESSING_EVENTS:
                key, reason = SUPPRESSING_EVENTS[event]
                recipients = _recipients(payload, key)
                for addr in recipients:
                    consent.suppress(addr, reason, db=s)
                return {"duplicate": False, "event": event, "status": "suppressed", "suppressed": len(recipients)}
            if event != "message.received":
                return {"duplicate": False, "event": event, "status": "ignored"}
            receipt.event_id = publish("email.received", _email_payload(payload), source="agentmail", session=s)
            event_id = receipt.event_id
    except IntegrityError:
        return {"duplicate": True, "event": event}
    except Exception as exc:
        record_error("webhook.agentmail", exc, context={"event": event, "svix_id": msg_id})
        raise HTTPException(500, "processing failed; retry") from exc
    return {"duplicate": False, "event": event, "event_id": event_id}


@router.post("/agentmail")
async def agentmail_webhook(request: Request, bg: BackgroundTasks) -> dict[str, Any]:
    """AgentMail ``message.received`` webhook -> email.received event for the Support agent;
    ``message.bounced`` / ``message.complained`` -> the recipients are suppressed for marketing email."""
    raw = await request.body()
    h = request.headers
    result = await run_in_threadpool(_agentmail, raw, h.get("svix-id"), h.get("svix-timestamp"),
                                     h.get("svix-signature"))
    if result.get("event_id"):
        bg.add_task(drain)
    return result


# --------------------------------------------------------------------------- WhatsApp (Meta Cloud API)
# Disabled (404 on both routes, same as a URL nobody registered) until ``whatsapp_enabled`` and all four
# ``WHATSAPP_*`` secrets are set (``aecom.channels.whatsapp.WhatsAppChannel.enabled``) - see that module's
# docstring. Unlike Lemon Squeezy/AgentMail above, a delivery can batch several messages; each gets its own
# ``WebhookReceipt`` (keyed by the WhatsApp message id) so a provider retry of a partially-processed batch only
# replays the messages not yet recorded. A message that fails after its receipt is recorded (a `router.py` or
# `chat.py` bug, not a signature/delivery problem) is logged via ``record_error`` rather than retried: WhatsApp
# would otherwise redeliver the whole batch and risk sending a reply twice for the messages that *did* succeed,
# exactly what Telegram's ``poll_updates`` also avoids by advancing its offset over every update it saw.


@router.get("/whatsapp")
async def whatsapp_verify(request: Request) -> Response:
    """Meta's handshake, sent once when the webhook URL is (re)registered: echo ``hub.challenge`` back as plain
    text if ``hub.mode=subscribe`` and ``hub.verify_token`` matches ``WHATSAPP_VERIFY_TOKEN``, else refuse.
    (https://developers.facebook.com/docs/graph-api/webhooks/getting-started#verification-requests)"""
    if not wa.CHANNEL.enabled():
        raise HTTPException(404, "whatsapp channel disabled")
    params = request.query_params
    verify_token = secrets.get("WHATSAPP_VERIFY_TOKEN") or ""
    sent_token = params.get("hub.verify_token") or ""
    if params.get("hub.mode") != "subscribe" or not verify_token or not hmac.compare_digest(sent_token, verify_token):
        raise HTTPException(403, "hub.verify_token mismatch")
    return Response(content=params.get("hub.challenge", ""), media_type="text/plain")


def _whatsapp(raw: bytes, signature: str | None) -> dict[str, Any]:
    if not wa.CHANNEL.enabled():
        raise HTTPException(404, "whatsapp channel disabled")
    app_secret = secrets.get("WHATSAPP_APP_SECRET")
    if not app_secret or not wa.verify_signature(raw, signature, app_secret):
        raise HTTPException(401, "invalid or missing X-Hub-Signature-256")
    payload = _json(raw)
    processed = 0
    duplicates = 0
    for msg in wa.iter_inbound(payload):
        event_key = msg.message_id or hashlib.sha256(f"{msg.external_chat_id}:{msg.text}:{msg.callback_data}"
                                                      .encode()).hexdigest()
        try:
            with session() as s:  # the receipt alone: see the section docstring for why processing is not in here
                s.add(WebhookReceipt(provider="whatsapp", event_key=event_key, topic="message"))
                s.flush()
        except IntegrityError:
            duplicates += 1
            continue
        try:
            for reply in whatsapp_handle_inbound(msg):
                wa.CHANNEL.send(msg.external_chat_id, reply.text, reply.buttons)
        except Exception as exc:  # noqa: BLE001 - logged, not retried; see the section docstring above
            record_error("webhook.whatsapp", exc, context={"message_id": msg.message_id})
        processed += 1
    return {"duplicate": processed == 0 and duplicates > 0, "processed": processed, "duplicates": duplicates}


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request, bg: BackgroundTasks) -> dict[str, Any]:
    """WhatsApp Cloud API message webhook: ``X-Hub-Signature-256`` HMAC check, one ``WebhookReceipt`` per message
    id, inbound text/button-tap routed through ``channels.router.handle_inbound``, replies sent back through the
    Graph API (``channels.whatsapp.CHANNEL.send``)."""
    raw = await request.body()
    result = await run_in_threadpool(_whatsapp, raw, request.headers.get("x-hub-signature-256"))
    if result["processed"]:
        bg.add_task(drain)
    return result
