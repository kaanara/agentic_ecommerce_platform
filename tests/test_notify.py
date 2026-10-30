"""notify.route (Telegram/ntfy/dashboard-only, quiet hours) and the message_owner tool."""

from __future__ import annotations

import httpx
import pytest

from aecom import conversations, notify
from aecom.config import get_settings
from aecom.context import current_agent
from aecom.db import ChannelLink, session
from aecom.secrets import secrets
from aecom.tools.catalog import message_owner


@pytest.fixture(autouse=True)
def no_env_secrets(monkeypatch):
    """No ntfy config leaking from the real environment; tests opt in explicitly."""
    values: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get", lambda name: values.get(name))
    return values


@pytest.fixture
def net(monkeypatch):
    """Records every httpx POST notify.route makes; `net.fail = True` makes them raise (net_guard: no real HTTP)."""
    class Net:
        calls: list[tuple] = []
        fail = False

    def fake_post(url, **kw):
        Net.calls.append((url, kw))
        if Net.fail:
            raise httpx.ConnectError(f"cannot reach {url}")
        return httpx.Response(200, request=httpx.Request("POST", url))

    Net.calls = []
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    return Net


def _pair(chat_id: str = "123") -> None:
    with session() as s:
        s.add(ChannelLink(channel="telegram", external_chat_id=chat_id, active=True))


def test_route_dashboard_only_without_any_channel():
    result = notify.route({"kind": "message", "text": "hello", "urgency": "normal"})
    assert result == {"dashboard": True, "telegram": False, "ntfy": False}


def test_route_falls_back_to_ntfy_when_not_paired(net):
    secrets_values = {"NTFY_URL": "https://ntfy.example/topic"}
    import aecom.secrets as secrets_mod

    secrets_mod.secrets.get = lambda name: secrets_values.get(name)
    result = notify.route({"kind": "message", "text": "hi there", "urgency": "normal"})
    assert result["telegram"] is False
    assert result["ntfy"] is True
    assert len(net.calls) == 1


def test_route_prefers_telegram_when_paired(monkeypatch, net):
    _pair("999")
    sent: list[tuple] = []
    notify.set_telegram_sender(lambda chat_id, text: sent.append((chat_id, text)) or True)
    try:
        result = notify.route({"kind": "message", "text": "paired message", "urgency": "normal"})
    finally:
        notify.set_telegram_sender(None)
    assert result["telegram"] is True
    assert result["ntfy"] is False
    assert sent == [("999", "paired message")]
    assert net.calls == []  # ntfy never called once Telegram delivered


def test_route_falls_back_to_ntfy_when_telegram_sender_fails(net):
    _pair("999")
    notify.set_telegram_sender(lambda chat_id, text: False)
    secrets_values = {"NTFY_URL": "https://ntfy.example/topic"}
    import aecom.secrets as secrets_mod

    secrets_mod.secrets.get = lambda name: secrets_values.get(name)
    try:
        result = notify.route({"kind": "message", "text": "retry via ntfy", "urgency": "normal"})
    finally:
        notify.set_telegram_sender(None)
    assert result["telegram"] is False
    assert result["ntfy"] is True


def test_quiet_hours_block_normal_urgency(net):
    s = get_settings()
    now = __import__("datetime").datetime.now()
    s.quiet_hours_start, s.quiet_hours_end = "00:00", "23:59"
    try:
        result = notify.route({"kind": "message", "text": "shh", "urgency": "normal"})
    finally:
        s.quiet_hours_start, s.quiet_hours_end = "", ""
    assert result == {"dashboard": True, "telegram": False, "ntfy": False}
    assert net.calls == []


def test_quiet_hours_ignored_for_high_urgency(net):
    s = get_settings()
    secrets_values = {"NTFY_URL": "https://ntfy.example/topic"}
    import aecom.secrets as secrets_mod

    secrets_mod.secrets.get = lambda name: secrets_values.get(name)
    s.quiet_hours_start, s.quiet_hours_end = "00:00", "23:59"
    try:
        result = notify.route({"kind": "incident", "text": "P1!", "urgency": "high"})
    finally:
        s.quiet_hours_start, s.quiet_hours_end = "", ""
    assert result["ntfy"] is True


def test_telegram_module_sender_is_preferred(monkeypatch, net):
    """If aecom.channels.telegram exists and exposes send(), it wins over the fallback hook."""
    _pair("555")
    from aecom.channels import telegram

    monkeypatch.setattr(telegram, "send", lambda chat_id, text: True)
    fallback_called = []
    notify.set_telegram_sender(lambda chat_id, text: fallback_called.append(1) or True)
    try:
        result = notify.route({"kind": "message", "text": "via module", "urgency": "normal"})
    finally:
        notify.set_telegram_sender(None)
    assert result["telegram"] is True
    assert fallback_called == []


# --------------------------------------------------------------------------- message_owner tool


def _as(agent_id: str):
    token = current_agent.set(agent_id)
    return token


def test_message_owner_writes_conversation_and_routes(net):
    token = _as("ceo")
    try:
        out = message_owner("Please review the new OKR", urgency="normal", question=True)
    finally:
        current_agent.reset(token)
    assert out["sent"] is True
    conv = conversations.get(out["conversation_id"])
    assert conv["agent_id"] == "ceo"
    assert conv["started_by"] == "agent"
    assert conv["unread"] == 1
    assert conv["messages"][0]["content"] == "Please review the new OKR"
    assert out["delivered"]["dashboard"] is True


def test_message_owner_rejects_bad_urgency():
    token = _as("ceo")
    try:
        with pytest.raises(ValueError):
            message_owner("x", urgency="urgent")
    finally:
        current_agent.reset(token)


def test_message_owner_requires_agent_context():
    token = current_agent.set("")
    try:
        with pytest.raises(ValueError):
            message_owner("no agent set")
    finally:
        current_agent.reset(token)


def test_message_owner_rate_limited_per_agent_per_day(net):
    s = get_settings()
    saved = s.owner_message_daily_limit
    s.owner_message_daily_limit = 2
    token = _as("finance")
    try:
        assert message_owner("one")["sent"] is True
        assert message_owner("two")["sent"] is True
        third = message_owner("three")
        assert third["sent"] is False
        assert "limit" in third["reason"]
    finally:
        current_agent.reset(token)
        s.owner_message_daily_limit = saved


def test_message_owner_limit_is_per_agent():
    s = get_settings()
    saved = s.owner_message_daily_limit
    s.owner_message_daily_limit = 1
    try:
        t1 = _as("ceo")
        try:
            assert message_owner("ceo message")["sent"] is True
        finally:
            current_agent.reset(t1)
        t2 = _as("finance")
        try:
            assert message_owner("finance message")["sent"] is True
        finally:
            current_agent.reset(t2)
    finally:
        s.owner_message_daily_limit = saved
