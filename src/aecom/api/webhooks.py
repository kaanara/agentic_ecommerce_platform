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

R14: ``GET /download/{order_id}`` is a second kind of public route, registered on ``download_router`` below (no
``/webhooks`` prefix - it is a customer-facing link, not a provider callback) but included on ``app`` the same way:
outside the bearer-token-protected ``api`` router, see the dedicated section near the bottom of this file.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from aecom import consent
from aecom.audit import audit
from aecom.bus.events import drain, publish
from aecom.channels import whatsapp as wa
from aecom.channels.router import handle_inbound as whatsapp_handle_inbound
from aecom.config import ROOT, get_settings
from aecom.db import AuditLog, Order, Product, WebhookReceipt, now, session
from aecom.observability import record_error
from aecom.secrets import secrets
from aecom.store import lemonsqueezy as ls
from aecom.store.adapter import get_store

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


# --------------------------------------------------------------------------- customer downloads (R14)
# Public (no bearer token - a customer's browser, not an agent or a dashboard) and, like WhatsApp above, 404 while
# disabled: there is no public URL for this host yet (the Pi sits behind no tunnel), so going live needs the move
# to a cloud server (U2's trigger). The lead must add two fields to ``config.Settings`` plus matching
# ``settings_registry`` rows; until then this module reads them defensively with ``getattr(..., default)``:
#
#   download_route_enabled: bool = False   # config.py; settings_registry: kind="bool", section="Store and email",
#                                           # risk="sensitive" (same shape as whatsapp_enabled)
#   downloads_dir: Path = ROOT / "data" / "downloads"  # config.py; settings_registry: kind="path",
#                                           # section="Store and email" (files served by the route live here)
#
# Auth is the order's own signed token (``StoreAdapter.verify_download``, constant-time compare), never the bearer
# token. Every failure the customer can trigger - disabled, unknown/foreign order, bad/expired token, a refunded
# order, a product with no (or an unsafe) file on disk - answers the identical 404, so a guess can never learn
# whether an order id exists (no oracle). Rate limiting alone answers 429: it reveals nothing about any particular
# order, only that this IP is hammering one. Every attempt (allowed, denied or rate-limited) is one audit row;
# the token itself is never included (``audit.mask_detail`` would redact it anyway, see ``REDACT_KEYS``).
download_router = APIRouter(tags=["downloads"])

DOWNLOAD_RATE_LIMIT = 10          # attempts
DOWNLOAD_RATE_WINDOW_S = 60       # per this many seconds, per (ip, order_id)


def _download_route_enabled() -> bool:
    return bool(getattr(get_settings(), "download_route_enabled", False))


def _downloads_dir() -> Path:
    return Path(getattr(get_settings(), "downloads_dir", ROOT / "data" / "downloads"))


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _download_attempts(ip: str, order_id: str) -> int:
    """Attempts from this IP at this order in the trailing window (I16-style audit rows double as the limiter's
    storage: no extra table, no in-process state to lose on restart or disagree across workers)."""
    cutoff = now() - timedelta(seconds=DOWNLOAD_RATE_WINDOW_S)
    with session() as s:
        return s.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.actor == f"download:{ip}:{order_id}", AuditLog.action == "download_attempt",
            AuditLog.ts >= cutoff)) or 0


def _audit_download(ip: str, order_id: str, decision: str, reason: str = "") -> None:
    audit(f"download:{ip}:{order_id}", "download_attempt", decision, {"order_id": order_id, "reason": reason})


def _safe_asset_path(downloads_dir: Path, asset_url: str | None) -> Path | None:
    """The product's file, resolved strictly inside ``downloads_dir``. ``None`` for anything else a product's
    ``asset_url`` might hold (empty, a remote URL/URI such as ``s3://...`` - not ours to stream -, an absolute
    path, or ``..`` segments) or for a path that does not resolve to an existing file."""
    if not asset_url or "://" in asset_url:
        return None
    rel = PurePosixPath(asset_url.replace("\\", "/").lstrip("/"))
    if rel.is_absolute() or not rel.parts or any(part in ("..", "") for part in rel.parts):
        return None
    base = downloads_dir.resolve()
    candidate = (base / Path(*rel.parts)).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        return None
    return candidate


@download_router.get("/download/{order_id}")
def download_product(order_id: str, token: str, request: Request) -> FileResponse:
    """Streams the order's product file once ``verify_download`` accepts ``token`` (I13's download link).
    404 while disabled, or for any failure (invalid/expired/refunded token, missing/unsafe file) - the same
    response for all of them, on purpose. 429 (not 404) when this IP is over the per-order rate limit."""
    if not _download_route_enabled():
        raise HTTPException(404, "not found")
    ip = _client_ip(request)
    if _download_attempts(ip, order_id) >= DOWNLOAD_RATE_LIMIT:
        _audit_download(ip, order_id, "deny", "rate_limited")
        raise HTTPException(429, "too many requests")
    if not get_store().verify_download(order_id, token):
        _audit_download(ip, order_id, "deny", "invalid_token")
        raise HTTPException(404, "not found")
    with session() as s:
        order = s.get(Order, order_id)
        product = s.get(Product, order.product_id) if order else None
    path = _safe_asset_path(_downloads_dir(), product.asset_url if product else None)
    if path is None:
        _audit_download(ip, order_id, "deny", "no_file")
        raise HTTPException(404, "not found")
    _audit_download(ip, order_id, "allow")
    filename = (product.sku + path.suffix) if product else path.name
    return FileResponse(path, media_type="application/octet-stream", filename=filename)
