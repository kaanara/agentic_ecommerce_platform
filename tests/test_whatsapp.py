"""Plan section 3 (WhatsApp) / W4: Cloud API adapter, behind `GET`/`POST /webhooks/whatsapp`.

Offline only: scripted LLM (conftest.py), no network - replies are sent via `WhatsAppChannel.send`, which is
monkeypatched here to a recorder instead of actually calling the Graph API (so these tests never need `net_guard`
to reach the internet; `net_guard`'s own SSRF behaviour is covered by test_security.py). Fixtures are recorded
Cloud API webhook deliveries under tests/fixtures/whatsapp/.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aecom import dashboard_auth
from aecom.api import app as api_app
from aecom.api import webhooks
from aecom.channels import router
from aecom.channels import whatsapp as wa
from aecom.config import get_settings
from aecom.db import AuditLog, ChannelLink, WebhookReceipt, session

FIX = Path(__file__).parent / "fixtures" / "whatsapp"
APP_SECRET = "wa-test-app-secret"
VERIFY_TOKEN = "wa-test-verify-token"
TOKEN = "wa-test-access-token"
PHONE_ID = "123456123"

ALL_SECRETS = {"WHATSAPP_TOKEN": TOKEN, "WHATSAPP_PHONE_ID": PHONE_ID, "WHATSAPP_APP_SECRET": APP_SECRET,
              "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN}


def body_of(name: str) -> bytes:
    return json.dumps(json.loads((FIX / f"{name}.json").read_text())).encode()


def sig(raw: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def count(model) -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def enabled(monkeypatch):
    """whatsapp_enabled on and every secret configured; secrets.get answers only from `configured`."""
    get_settings().whatsapp_enabled = True
    configured = dict(ALL_SECRETS)
    monkeypatch.setattr(wa.secrets, "get", lambda name: configured.get(name))
    monkeypatch.setattr(wa.secrets, "has", lambda name: bool(configured.get(name)))
    monkeypatch.setattr(webhooks.secrets, "get", lambda name: configured.get(name))
    monkeypatch.setattr(webhooks.secrets, "has", lambda name: bool(configured.get(name)))
    yield configured
    get_settings().whatsapp_enabled = False


@pytest.fixture
def client():
    return TestClient(api_app.app)


@pytest.fixture
def sent(monkeypatch):
    """Records every `WhatsAppChannel.send` call instead of reaching the Graph API."""
    calls: list[dict] = []
    monkeypatch.setattr(wa.CHANNEL, "send",
                        lambda chat_id, text, buttons=None: calls.append({"chat_id": chat_id, "text": text,
                                                                          "buttons": buttons}) or True)
    return calls


def _pair(wa_id: str = "16315551234") -> None:
    with session() as s:
        s.add(ChannelLink(channel="whatsapp", external_chat_id=wa_id, paired_by="tester"))


def _login_on():
    dashboard_auth.set_password("a-strong-password-123", "a-strong-password-123", "tester")


# --------------------------------------------------------------------------- disabled by default


def test_disabled_by_default_both_routes_404(client):
    assert not wa.CHANNEL.enabled()
    r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                 "hub.challenge": "1234"})
    assert r.status_code == 404
    raw = body_of("inbound_text")
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw)})
    assert r.status_code == 404
    assert count(WebhookReceipt) == 0


def test_disabled_when_a_required_secret_is_missing(client, enabled):
    enabled.pop("WHATSAPP_APP_SECRET")  # whatsapp_enabled is on but the channel still needs every secret
    assert not wa.CHANNEL.enabled()
    raw = body_of("inbound_text")
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw)})
    assert r.status_code == 404


# --------------------------------------------------------------------------- GET handshake


def test_handshake_echoes_challenge_when_verify_token_matches(client, enabled):
    r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                                                 "hub.challenge": "1858565365"})
    assert r.status_code == 200
    assert r.text == "1858565365"
    assert r.headers["content-type"].startswith("text/plain")


def test_handshake_rejects_wrong_verify_token_or_mode(client, enabled):
    r = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                                 "hub.challenge": "1"})
    assert r.status_code == 403
    r = client.get("/webhooks/whatsapp", params={"hub.mode": "unsubscribe", "hub.verify_token": VERIFY_TOKEN,
                                                 "hub.challenge": "1"})
    assert r.status_code == 403
    r = client.get("/webhooks/whatsapp")  # no params at all
    assert r.status_code == 403


# --------------------------------------------------------------------------- POST signature + idempotency


def test_valid_signature_routes_inbound_text_and_replies(client, enabled, sent, monkeypatch):
    _login_on()
    _pair()
    seen = {}

    def fake_converse(conversation_id, user_text, **kw):
        seen.update(conversation_id=conversation_id, user_text=user_text)
        from aecom.chat import ChatTurn

        return ChatTurn(conversation_id, 1, "ceo", "business is good", status="ok")

    monkeypatch.setattr(router.chat, "converse", fake_converse)
    raw = body_of("inbound_text")
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"duplicate": False, "processed": 1, "duplicates": 0}
    assert seen["user_text"] == "how's business?"
    with session() as s:
        receipt = s.scalars(select(WebhookReceipt)).one()
        assert receipt.provider == "whatsapp"
        assert receipt.event_key == "wamid.HBgLMTYzMTU1NTEyMzQVAgARGBI5QTNDQTVCM0Q0RTdGRTM1RTkA"
    assert sent == [{"chat_id": "16315551234", "text": "business is good", "buttons": None}]


def test_bad_or_missing_signature_is_401_and_writes_nothing(client, enabled, sent):
    raw = body_of("inbound_text")
    before = count(WebhookReceipt)
    r = client.post("/webhooks/whatsapp", content=raw)  # no signature header at all
    assert r.status_code == 401
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": "sha256=" + "0" * 64})
    assert r.status_code == 401
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw, "other-secret")})
    assert r.status_code == 401
    r = client.post("/webhooks/whatsapp", content=raw + b" ", headers={"x-hub-signature-256": sig(raw)})
    assert r.status_code == 401  # signature is over the raw body
    assert count(WebhookReceipt) == before
    assert sent == []


def test_duplicate_message_id_is_ignored_without_side_effects(client, enabled, sent):
    _login_on()
    _pair()
    raw = body_of("inbound_text")
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw)})
    assert r.status_code == 200 and r.json()["duplicate"] is False
    sent.clear()
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw)})  # Meta's retry
    assert r.status_code == 200
    assert r.json() == {"duplicate": True, "processed": 0, "duplicates": 1}
    assert sent == []  # no second reply sent
    assert count(WebhookReceipt) == 1


def test_status_update_payload_has_no_messages_and_is_a_no_op(client, enabled, sent):
    raw = body_of("status_update")
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw)})
    assert r.status_code == 200
    assert r.json() == {"duplicate": False, "processed": 0, "duplicates": 0}
    assert count(WebhookReceipt) == 0
    assert sent == []


# --------------------------------------------------------------------------- unpaired sender


def test_unpaired_sender_is_ignored_and_audited(client, enabled, sent):
    raw = body_of("inbound_unpaired")
    r = client.post("/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": sig(raw)})
    assert r.status_code == 200
    assert r.json() == {"duplicate": False, "processed": 1, "duplicates": 0}
    assert sent == []  # nothing sent back to an unpaired chat
    with session() as s:
        row = s.query(AuditLog).filter_by(action="channel_inbound_unpaired").order_by(AuditLog.id.desc()).first()
        assert row is not None
        assert row.detail["channel"] == "whatsapp" and row.detail["external_chat_id"] == "19995550000"
    with session() as s:
        assert s.query(ChannelLink).filter_by(channel="whatsapp", external_chat_id="19995550000").first() is None


# --------------------------------------------------------------------------- signature / parsing helpers


def test_verify_signature_rejects_missing_or_wrong_prefix():
    raw = b'{"a": 1}'
    assert not wa.verify_signature(raw, None, APP_SECRET)
    assert not wa.verify_signature(raw, "md5=deadbeef", APP_SECRET)
    assert wa.verify_signature(raw, sig(raw), APP_SECRET)


def test_iter_inbound_parses_text_messages():
    payload = json.loads(body_of("inbound_text"))
    messages = wa.iter_inbound(payload)
    assert len(messages) == 1
    assert messages[0].channel == "whatsapp"
    assert messages[0].external_chat_id == "16315551234"
    assert messages[0].text == "how's business?"
    assert messages[0].message_id.startswith("wamid.")


def test_iter_inbound_ignores_status_only_payloads():
    payload = json.loads(body_of("status_update"))
    assert wa.iter_inbound(payload) == []


def test_interactive_button_reply_becomes_callback_data():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "16315551234", "id": "wamid.btn1", "type": "interactive",
         "interactive": {"type": "button_reply", "button_reply": {"id": "approve:appr_1", "title": "Approve"}}}
    ]}}]}]}
    messages = wa.iter_inbound(payload)
    assert len(messages) == 1
    assert messages[0].callback_data == "approve:appr_1"
    assert messages[0].text == ""


def test_message_body_uses_interactive_for_buttons_and_text_otherwise():
    from aecom.channels.base import Button

    plain = wa._message_body("123", "hi", None)
    assert plain == {"messaging_product": "whatsapp", "to": "123", "type": "text", "text": {"body": "hi"}}
    with_buttons = wa._message_body("123", "pick one", [Button("Approve", "approve:1"), Button("Reject", "reject:1")])
    assert with_buttons["type"] == "interactive"
    assert len(with_buttons["interactive"]["action"]["buttons"]) == 2
    assert with_buttons["interactive"]["action"]["buttons"][0] == {"type": "reply",
                                                                   "reply": {"id": "approve:1", "title": "Approve"}}


def test_channel_disabled_without_settings_or_secrets(monkeypatch):
    assert wa.CHANNEL.enabled() is False
    get_settings().whatsapp_enabled = True
    try:
        assert wa.CHANNEL.enabled() is False  # settings on but no secrets
    finally:
        get_settings().whatsapp_enabled = False


def test_send_returns_false_without_token_or_phone_id(enabled):
    enabled.pop("WHATSAPP_TOKEN")
    assert wa.CHANNEL.send("123", "hi") is False
