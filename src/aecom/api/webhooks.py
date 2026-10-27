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
Each delivery is recorded once in ``WebhookReceipt`` (unique ``(provider, event_key)``); a repeat returns
200 ``{"duplicate": true}`` without side effects. Only these routes may be reachable from the internet (via a
tunnel); the dashboard and the rest of the API must never be exposed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from aecom.bus.events import drain, publish
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


def _claim(provider: str, event_key: str, topic: str) -> int | None:
    """Insert the receipt in its own committed transaction. None = this delivery was already received."""
    try:
        with session() as s:
            row = WebhookReceipt(provider=provider, event_key=event_key, topic=topic)
            s.add(row)
            s.flush()
            return row.id
    except IntegrityError:
        return None


def _release(receipt_id: int) -> None:
    """Undo a claim after a failed delivery so the provider's retry is processed."""
    with session() as s:
        s.execute(delete(WebhookReceipt).where(WebhookReceipt.id == receipt_id))


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
    receipt_id = _claim(ls.PROVIDER, event_key, event)
    if receipt_id is None:
        return {"duplicate": True, "event": event}
    try:
        result = ls.dispatch(payload)
    except Exception as exc:
        _release(receipt_id)
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
    """AgentMail ``message.received`` webhook -> email.received event for the Support agent."""
    raw = await request.body()
    h = request.headers
    result = await run_in_threadpool(_agentmail, raw, h.get("svix-id"), h.get("svix-timestamp"),
                                     h.get("svix-signature"))
    if result.get("event_id"):
        bg.add_task(drain)
    return result
