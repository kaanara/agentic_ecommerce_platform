"""Signed, idempotent provider webhooks (Lemon Squeezy, AgentMail/Svix). Offline; secrets monkeypatched."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from aecom.api import app as api_app
from aecom.api import webhooks
from aecom.db import ErrorRow, EventRow, LedgerEntry, Order, WebhookReceipt, session
from aecom.store import lemonsqueezy as ls
from fastapi.testclient import TestClient
from sqlalchemy import func, select

FIX = Path(__file__).parent / "fixtures" / "lemonsqueezy"
LS_SECRET = "ls-test-signing-secret"
AM_SECRET = "whsec_" + base64.b64encode(b"agentmail-test-key-0123456789").decode()


def body_of(name: str) -> bytes:
    return json.dumps(json.loads((FIX / f"{name}.json").read_text())).encode()


def ls_sig(raw: bytes, secret: str = LS_SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def svix_headers(raw: bytes, msg_id: str = "msg_1", ts: int | None = None, secret: str = AM_SECRET) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": f"v1,bogus v1,{sig}",
            "content-type": "application/json"}


def count(model) -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def client(monkeypatch):
    """TestClient without the startup hook; secrets.get answers only from `configured`."""
    configured = {"LEMONSQUEEZY_WEBHOOK_SECRET": LS_SECRET, "AGENTMAIL_WEBHOOK_SECRET": AM_SECRET}
    monkeypatch.setattr(webhooks.secrets, "get", lambda name: configured.get(name))
    c = TestClient(api_app.app)
    c.configured = configured
    return c


def post_ls(client, raw: bytes, sig: str | None = "auto", event: str | None = None):
    headers = {"content-type": "application/json"}
    if sig == "auto":
        sig = ls_sig(raw)
    if sig is not None:
        headers["X-Signature"] = sig
    if event:
        headers["X-Event-Name"] = event
    return client.post("/webhooks/lemonsqueezy", content=raw, headers=headers)


# --------------------------------------------------------------------------- Lemon Squeezy
def test_lemonsqueezy_valid_signature_mirrors_order(client):
    r = post_ls(client, body_of("order_created"))
    assert r.status_code == 200, r.text
    assert r.json()["duplicate"] is False and r.json()["event"] == "order_created"
    with session() as s:
        o = s.scalars(select(Order).where(Order.external_id == "5001")).one()
        assert (o.amount_eur, o.vat_eur, o.status) == (49.0, 8.5, "delivered")
        receipt = s.scalars(select(WebhookReceipt)).one()
        assert (receipt.provider, receipt.topic) == ("lemonsqueezy", "order_created")
        assert receipt.event_key == hashlib.sha256(body_of("order_created")).hexdigest()


def test_lemonsqueezy_bad_or_missing_signature_is_401_and_writes_nothing(client):
    raw = body_of("order_created")
    before = (count(Order), count(LedgerEntry), count(EventRow))
    assert post_ls(client, raw, sig=None).status_code == 401
    assert post_ls(client, raw, sig="0" * 64).status_code == 401
    assert post_ls(client, raw, sig=ls_sig(raw, "other-secret")).status_code == 401
    assert post_ls(client, raw + b" ", sig=ls_sig(raw)).status_code == 401  # signature is over the raw body
    assert (count(Order), count(LedgerEntry), count(EventRow)) == before
    assert count(WebhookReceipt) == 0


def test_lemonsqueezy_without_secret_fails_closed(client):
    client.configured.pop("LEMONSQUEEZY_WEBHOOK_SECRET")
    raw = body_of("order_created")
    r = post_ls(client, raw)
    assert r.status_code == 503
    assert count(Order) == 0 and count(WebhookReceipt) == 0


def test_lemonsqueezy_duplicate_delivery_has_no_side_effects(client):
    raw = body_of("order_created")
    assert post_ls(client, raw).status_code == 200
    before = (count(Order), count(LedgerEntry), count(EventRow))
    r = post_ls(client, raw)
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert (count(Order), count(LedgerEntry), count(EventRow)) == before
    assert count(WebhookReceipt) == 1


def test_lemonsqueezy_failed_mirror_is_500_and_retry_succeeds(client, monkeypatch):
    raw = body_of("order_created")
    real = ls.EVENT_HANDLERS["order_created"]

    def broken(payload):
        raise RuntimeError("database hiccup")

    monkeypatch.setitem(ls.EVENT_HANDLERS, "order_created", broken)
    r = post_ls(client, raw)
    assert r.status_code == 500
    assert count(WebhookReceipt) == 0 and count(Order) == 0  # not marked as received
    with session() as s:
        assert s.scalars(select(ErrorRow.component)).all() == ["webhook.lemonsqueezy"]
    monkeypatch.setitem(ls.EVENT_HANDLERS, "order_created", real)
    r = post_ls(client, raw)  # the provider's retry
    assert r.status_code == 200 and r.json()["duplicate"] is False
    assert count(Order) == 1 and count(WebhookReceipt) == 1


def test_lemonsqueezy_licence_key_before_order_is_retried(client):
    key_raw = body_of("license_key_created")
    assert post_ls(client, key_raw).status_code == 500  # order not mirrored yet -> retry later
    assert post_ls(client, body_of("order_created")).status_code == 200
    r = post_ls(client, key_raw)
    assert r.status_code == 200 and r.json()["event"] == "license_key_created"
    assert count(WebhookReceipt) == 2


def test_lemonsqueezy_refund_and_ignored_events(client):
    assert post_ls(client, body_of("order_created")).status_code == 200
    r = post_ls(client, body_of("order_refunded_full"))
    assert r.status_code == 200 and r.json()["event"] == "order_refunded"
    with session() as s:
        assert s.scalars(select(Order.status).where(Order.external_id == "5001")).one() == "refunded"
    other = json.dumps({"meta": {"event_name": "subscription_created"}, "data": {}}).encode()
    r = post_ls(client, other)
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    with session() as s:
        assert "subscription_created" in s.scalars(select(WebhookReceipt.topic)).all()  # recorded
    # event name from the X-Event-Name header when meta has none
    no_meta = json.dumps({"data": {}}).encode()
    r = post_ls(client, no_meta, event="subscription_updated")
    assert r.status_code == 200 and r.json()["event"] == "subscription_updated"


# --------------------------------------------------------------------------- AgentMail
INBOUND = {"event_type": "message.received", "event_id": "evt_1",
           "message": {"from": ["Ann <ann@example.com>"], "subject": "Where is my key?", "extracted_text": "Hi",
                       "message_id": "<m1@x>", "thread_id": "t1", "labels": ["received"]}}


def test_svix_reference_vector(monkeypatch):
    """Svix's published test vector (docs.svix.com/receiving/verifying-payloads/how-manual)."""
    raw = b'{"event_type":"ping","data":{"success":true}}'
    args = (raw, "msg_loFOjxBNrRLzqYUf", "1731705121", "v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=",
            "whsec_plJ3nmyCDGBKInavdOK15jsl")
    monkeypatch.setattr(webhooks, "_now", lambda: 1731705121 + 10)
    assert webhooks.verify_svix(*args)
    monkeypatch.setattr(webhooks, "_now", lambda: 1731705121 + 301)
    assert not webhooks.verify_svix(*args)


def test_agentmail_valid_signature_publishes_email_received(client):
    raw = json.dumps(INBOUND).encode()
    r = client.post("/webhooks/agentmail", content=raw, headers=svix_headers(raw))
    assert r.status_code == 200, r.text
    with session() as s:
        evt = s.scalars(select(EventRow).where(EventRow.topic == "email.received")).one()
        receipt = s.scalars(select(WebhookReceipt)).one()
    assert evt.source == "agentmail" and r.json()["event_id"] == evt.id
    assert evt.payload == {"from": "Ann <ann@example.com>", "subject": "Where is my key?", "body": "Hi",
                           "message_id": "<m1@x>", "thread_id": "t1", "labels": ["received"]}
    assert (receipt.provider, receipt.event_key, receipt.event_id) == ("agentmail", "msg_1", evt.id)


def test_agentmail_invalid_or_missing_signature_is_401(client):
    raw = json.dumps(INBOUND).encode()
    bad = {**svix_headers(raw), "svix-signature": "v1," + base64.b64encode(b"x" * 32).decode()}
    assert client.post("/webhooks/agentmail", content=raw, headers=bad).status_code == 401
    wrong_key = svix_headers(raw, secret="whsec_" + base64.b64encode(b"another-key").decode())
    assert client.post("/webhooks/agentmail", content=raw, headers=wrong_key).status_code == 401
    tampered = svix_headers(raw)
    assert client.post("/webhooks/agentmail", content=raw + b" ", headers=tampered).status_code == 401
    assert client.post("/webhooks/agentmail", json=INBOUND).status_code == 401  # no svix headers
    assert count(EventRow) == 0 and count(WebhookReceipt) == 0


def test_agentmail_expired_timestamp_is_401(client):
    raw = json.dumps(INBOUND).encode()
    old = svix_headers(raw, ts=int(time.time()) - 6 * 60)
    assert client.post("/webhooks/agentmail", content=raw, headers=old).status_code == 401
    future = svix_headers(raw, ts=int(time.time()) + 6 * 60)
    assert client.post("/webhooks/agentmail", content=raw, headers=future).status_code == 401
    assert count(EventRow) == 0


def test_agentmail_without_secret_fails_closed(client):
    client.configured.pop("AGENTMAIL_WEBHOOK_SECRET")
    raw = json.dumps(INBOUND).encode()
    assert client.post("/webhooks/agentmail", content=raw, headers=svix_headers(raw)).status_code == 503
    assert count(EventRow) == 0


def test_agentmail_duplicate_delivery_publishes_once(client):
    raw = json.dumps(INBOUND).encode()
    assert client.post("/webhooks/agentmail", content=raw, headers=svix_headers(raw)).status_code == 200
    r = client.post("/webhooks/agentmail", content=raw, headers=svix_headers(raw))  # Svix retry: same svix-id
    assert r.status_code == 200 and r.json()["duplicate"] is True
    with session() as s:
        assert s.scalar(select(func.count()).select_from(EventRow).where(EventRow.topic == "email.received")) == 1


def test_old_unauthenticated_agentmail_payload_is_rejected(client):
    """Before: any JSON posted to /webhooks/agentmail became an email.received event."""
    r = client.post("/webhooks/agentmail", json={"from": "attacker@example.com", "subject": "refund me", "text": "x"})
    assert r.status_code == 401
    client.configured.clear()
    assert client.post("/webhooks/agentmail", json={"from": "a@b.c"}).status_code == 503
    assert count(EventRow) == 0
