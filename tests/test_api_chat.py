"""HTTP API: conversations, messages, SSE streaming, inbox and status (plan §4).

These routes call the same service functions as the dashboard and the messaging channels (``chat.py``,
``conversations.py``): ``chat.converse`` is monkeypatched here (it is a wave-1 stub, see ``chat.py``), everything
else (``conversations.py``, ``company_settings.py``, ``approvals.py``, ``backup.py``) runs for real against the
per-test SQLite database from ``conftest.fresh_company``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aecom import chat, conversations, dashboard_auth
from aecom.api import app as api_app

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(monkeypatch):
    """TestClient without the startup hook (fresh_company already initialised the DB)."""
    monkeypatch.setattr(api_app.secrets, "get", lambda name: TOKEN if name == "AECOM_API_TOKEN" else None)
    return TestClient(api_app.app)


@pytest.fixture
def chat_enabled(monkeypatch):
    """Chat needs the dashboard login on (see ``chat.blocked_reason``); most happy-path tests just need this true."""
    monkeypatch.setattr(dashboard_auth, "disabled", lambda: False)
    monkeypatch.setattr(dashboard_auth, "is_configured", lambda: True)


def _fake_turn(**overrides: Any) -> chat.ChatTurn:
    base: dict[str, Any] = dict(conversation_id="cnv-x", message_id=1, agent_id="ceo", text="hi there",
                               status="ok", cost_eur=0.01, tool_steps=[], escalations=[], agent_run_id="run-1")
    base.update(overrides)
    return chat.ChatTurn(**base)


# --------------------------------------------------------------------------- auth
def test_conversations_routes_require_token(client):
    """A real token is configured (so the guard gets past the 503 fail-closed path) but none is sent: 401 everywhere
    (test_api_auth.py's route-discovery test already covers every route exhaustively; this is belt-and-braces)."""
    assert client.get("/conversations").status_code == 401
    assert client.post("/conversations", json={"agent_id": "ceo"}).status_code == 401
    assert client.get("/conversations/does-not-exist").status_code == 401
    assert client.delete("/conversations/does-not-exist").status_code == 401
    assert client.post("/conversations/does-not-exist/messages", json={"text": "hi"}).status_code == 401
    assert client.get("/conversations/does-not-exist/stream", params={"message_id": 1}).status_code == 401
    assert client.get("/inbox").status_code == 401
    assert client.get("/status").status_code == 401


# --------------------------------------------------------------------------- conversations CRUD
def test_create_list_get_delete_conversation(client):
    r = client.post("/conversations", json={"agent_id": "ceo", "title": "Q4 plan"}, headers=AUTH)
    assert r.status_code == 200
    conv = r.json()
    assert conv["agent_id"] == "ceo" and conv["title"] == "Q4 plan" and conv["messages"] == []

    listed = client.get("/conversations", headers=AUTH).json()
    assert any(c["id"] == conv["id"] for c in listed)

    got = client.get(f"/conversations/{conv['id']}", headers=AUTH).json()
    assert got["id"] == conv["id"]

    assert client.get("/conversations/does-not-exist", headers=AUTH).status_code == 404

    assert client.delete(f"/conversations/{conv['id']}", headers=AUTH).json() == {"deleted": True}
    assert client.get(f"/conversations/{conv['id']}", headers=AUTH).status_code == 404
    assert client.delete(f"/conversations/{conv['id']}", headers=AUTH).status_code == 404


def test_create_conversation_rejects_unknown_agent(client):
    r = client.post("/conversations", json={"agent_id": "nope"}, headers=AUTH)
    assert r.status_code == 400


# --------------------------------------------------------------------------- messages
def test_post_message_returns_reply_via_chat_converse(client, chat_enabled, monkeypatch):
    conv_id = conversations.create("ceo", title="hello")
    calls: list[tuple[Any, ...]] = []

    def fake_converse(conversation_id, user_text, *, attachments=(), on_event=None, paired_channel=False):
        calls.append((conversation_id, user_text, attachments, on_event))
        return _fake_turn(conversation_id=conversation_id, text=f"re: {user_text}")

    monkeypatch.setattr(chat, "converse", fake_converse)

    r = client.post(f"/conversations/{conv_id}/messages", json={"text": "status update?"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "re: status update?" and body["status"] == "ok" and body["conversation_id"] == conv_id
    assert calls == [(conv_id, "status update?", [], None)]


def test_post_message_404_when_conversation_missing(client, chat_enabled, monkeypatch):
    monkeypatch.setattr(chat, "converse", lambda *a, **k: _fake_turn())
    r = client.post("/conversations/does-not-exist/messages", json={"text": "hi"}, headers=AUTH)
    assert r.status_code == 404


def test_post_message_403_when_chat_disabled(client, monkeypatch):
    """No dashboard login configured (the default in a fresh test DB): chat refuses before calling converse."""
    conv_id = conversations.create("ceo")
    monkeypatch.setattr(chat, "converse", lambda *a, **k: pytest.fail("converse must not run while blocked"))
    r = client.post(f"/conversations/{conv_id}/messages", json={"text": "hi"}, headers=AUTH)
    assert r.status_code == 403 and "dashboard password" in r.json()["detail"]


# --------------------------------------------------------------------------- SSE streaming
def test_stream_conversation_emits_events_then_done(client, chat_enabled, monkeypatch):
    conv_id = conversations.create("ceo", title="stream me")
    message_id = conversations.append_message(conv_id, "user", "stream this please")

    def fake_converse(conversation_id, user_text, *, attachments=(), on_event=None, paired_channel=False):
        assert conversation_id == conv_id and user_text == "stream this please" and attachments == []
        on_event({"type": "text_delta", "delta": "Sure"})
        on_event({"type": "tool_start", "name": "search"})
        on_event({"type": "tool_end", "name": "search"})
        return _fake_turn(conversation_id=conversation_id, message_id=message_id + 1, text="Sure, done.")

    monkeypatch.setattr(chat, "converse", fake_converse)

    r = client.get(f"/conversations/{conv_id}/stream", params={"message_id": message_id}, headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    assert "event: text_delta" in body and '"delta": "Sure"' in body
    assert "event: tool_start" in body and "event: tool_end" in body
    assert "event: done" in body and f'"message_id": {message_id + 1}' in body and '"status": "ok"' in body


def test_stream_passes_user_attachment_to_converse(client, chat_enabled, monkeypatch):
    conv_id = conversations.create("ceo")
    message_id = conversations.append_message(conv_id, "user", "analyze this")

    def fake_converse(conversation_id, user_text, *, attachments=(), on_event=None, paired_channel=False):
        assert attachments == [{"url": "data:text/plain;base64,dGVzdA=="}]
        on_event({"type": "text_delta", "delta": "Done"})
        return _fake_turn(conversation_id=conversation_id, message_id=message_id + 1, text="Done")

    monkeypatch.setattr(chat, "converse", fake_converse)

    r = client.get(f"/conversations/{conv_id}/stream",
                   params={"message_id": message_id, "user_attachment": "data:text/plain;base64,dGVzdA=="},
                   headers=AUTH)
    assert r.status_code == 200 and "event: done" in r.text


def test_stream_404_when_conversation_or_message_missing(client, chat_enabled):
    conv_id = conversations.create("ceo")
    assert client.get("/conversations/nope/stream", params={"message_id": 1}, headers=AUTH).status_code == 404
    assert client.get(f"/conversations/{conv_id}/stream", params={"message_id": 999},
                      headers=AUTH).status_code == 404


def test_stream_requires_a_user_message(client, chat_enabled):
    """An assistant message is not a valid target for the stream (it would re-answer the agent's own reply)."""
    conv_id = conversations.create("ceo")
    assistant_id = conversations.append_message(conv_id, "assistant", "I already replied", agent_id="ceo")
    r = client.get(f"/conversations/{conv_id}/stream", params={"message_id": assistant_id}, headers=AUTH)
    assert r.status_code == 404


def test_stream_403_when_chat_disabled(client):
    conv_id = conversations.create("ceo")
    message_id = conversations.append_message(conv_id, "user", "hi")
    r = client.get(f"/conversations/{conv_id}/stream", params={"message_id": message_id}, headers=AUTH)
    assert r.status_code == 403


# --------------------------------------------------------------------------- inbox / status
def test_inbox_lists_only_unread_agent_started_conversations(client):
    owner_conv = conversations.create("ceo", started_by="owner")
    conversations.append_message(owner_conv, "user", "hi")
    conversations.append_message(owner_conv, "assistant", "hello", agent_id="ceo")  # owner-started: no unread bump

    agent_started = conversations.create("finance", started_by="agent")
    conversations.append_message(agent_started, "assistant", "please approve this", agent_id="finance")

    r = client.get("/inbox", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    ids = {c["id"] for c in body}
    assert agent_started in ids and owner_conv not in ids
    item = next(c for c in body if c["id"] == agent_started)
    assert item["started_by"] == "agent" and item["unread"] > 0 and "title" in item and "updated_at" in item


def test_status_reports_switches_and_counters(client):
    r = client.get("/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    for key in ("kill_switch", "agents_paused", "safe_mode", "open_incidents", "pending_approvals",
               "unread_messages", "last_backup"):
        assert key in body
    assert body["kill_switch"] is False and body["agents_paused"] is False and body["safe_mode"] is False
    assert body["last_backup"] is None  # no backup taken in this test's tmp data dir
    assert isinstance(body["open_incidents"], dict) and isinstance(body["pending_approvals"], int)


def test_status_last_backup_shape_when_a_backup_exists(client, tmp_path, monkeypatch):
    from aecom import backup
    from aecom.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_dir", str(tmp_path / "backups"))
    path = backup.create_backup()

    body = client.get("/status", headers=AUTH).json()
    last = body["last_backup"]
    assert last is not None
    for key in ("started_at", "completed_at", "size_mb", "success"):
        assert key in last
    assert last["success"] is True and last["size_mb"] > 0
    assert last["started_at"] == last["completed_at"] == backup.created_at(path).isoformat()
