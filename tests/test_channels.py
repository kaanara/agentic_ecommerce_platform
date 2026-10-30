"""Plan section 3 (Telegram): channel protocol, pairing, routing to commands or `chat.converse`, approval tier cap.

Offline only: scripted LLM (conftest.py), no network. `chat.converse` itself is monkeypatched here so these tests
stay focused on the router's own logic (pairing, commands, tier caps) - `chat.converse` is covered by test_chat.py.
"""

from __future__ import annotations

import json

import pytest

from aecom import chat, company_settings, dashboard_auth
from aecom.channels import router
from aecom.channels.base import Inbound
from aecom.db import Approval, ChannelLink, session


def _login_on():
    dashboard_auth.set_password("a-strong-password-123", "a-strong-password-123", "tester")


def _inbound(text: str = "", chat_id: str = "42", callback_data: str = "", callback_id: str = "") -> Inbound:
    return Inbound(channel="telegram", external_chat_id=chat_id, text=text,
                   callback_data=callback_data, callback_id=callback_id)


def _pair(chat_id: str = "42") -> None:
    with session() as s:
        s.add(ChannelLink(channel="telegram", external_chat_id=chat_id, paired_by="tester"))


def _approval(risk_tier: str = "T1", agent_id: str = "ceo") -> str:
    with session() as s:
        a = Approval(agent_id=agent_id, tool_name="fake_tool", args={"x": 1}, risk_tier=risk_tier, reason="why")
        s.add(a)
        s.flush()
        return a.id


# --------------------------------------------------------------------------- pairing


def test_pairing_refused_while_dashboard_login_is_off():
    dashboard_auth.turn_login_off("tester")
    with pytest.raises(PermissionError, match="dashboard login"):
        router.generate_pairing_code("owner")


def test_pair_with_valid_code_creates_an_active_link_and_is_one_time_use():
    _login_on()
    code = router.generate_pairing_code("owner")
    replies = router.handle_inbound(_inbound(f"/pair {code}"))
    assert any("Paired" in r.text for r in replies)
    with session() as s:
        link = s.query(ChannelLink).filter_by(channel="telegram", external_chat_id="42").one()
        assert link.active and link.max_approve_tier == "T1"
    # the code is one-time: pairing a second chat with it fails
    replies2 = router.handle_inbound(_inbound(f"/pair {code}", chat_id="99"))
    assert any("invalid or expired" in r.text for r in replies2)


def test_pair_with_wrong_or_expired_code_is_denied_and_audited():
    _login_on()
    router.generate_pairing_code("owner")
    replies = router.handle_inbound(_inbound("/pair 000000"))
    assert any("invalid or expired" in r.text for r in replies)
    with session() as s:
        assert s.query(ChannelLink).filter_by(channel="telegram", external_chat_id="42").first() is None


def test_unpaired_non_pair_messages_are_ignored_and_audited():
    _login_on()
    replies = router.handle_inbound(_inbound("hello there"))
    assert replies == []
    from aecom.db import AuditLog

    with session() as s:
        row = s.query(AuditLog).filter_by(action="channel_inbound_unpaired").order_by(AuditLog.id.desc()).first()
        assert row is not None and row.detail["channel"] == "telegram"


def test_pairing_a_new_chat_without_login_is_refused_even_with_a_stale_code(monkeypatch):
    """Login can be turned off again after a code was issued; pairing must still check at /pair time, not issue time."""
    _login_on()
    code = router.generate_pairing_code("owner")
    monkeypatch.setattr(dashboard_auth, "disabled", lambda: True)
    replies = router.handle_inbound(_inbound(f"/pair {code}"))
    assert any("login turned on" in r.text for r in replies)
    with session() as s:
        assert s.query(ChannelLink).filter_by(channel="telegram", external_chat_id="42").first() is None


# --------------------------------------------------------------------------- commands


def test_agent_command_rejects_unknown_agent_and_lists_available():
    _pair()
    replies = router.handle_inbound(_inbound("/agent nobody"))
    assert "Available" in replies[0].text
    for name in chat.chat_agents():
        assert name in replies[0].text


def test_agent_command_starts_a_new_conversation(monkeypatch):
    _pair()
    agent_id = chat.chat_agents()[0]
    replies = router.handle_inbound(_inbound(f"/agent {agent_id}"))
    assert f"Switched to {agent_id}" in replies[0].text
    session_data = json.loads(company_settings.get_setting("telegram_session:telegram:42"))
    assert session_data["agent_id"] == agent_id and session_data["conversation_id"]


def test_new_command_reuses_current_agent_or_falls_back_to_default():
    _pair()
    replies = router.handle_inbound(_inbound("/new"))
    assert "New chat started" in replies[0].text


def test_status_command_reports_switches_and_counts():
    _pair()
    replies = router.handle_inbound(_inbound("/status"))
    text = replies[0].text
    assert "kill switch" in text and "pending approvals" in text and "open incidents" in text


def test_plain_text_is_forwarded_to_chat_converse(monkeypatch):
    _pair()
    seen = {}

    def fake_converse(conversation_id, user_text, **kw):
        seen.update(conversation_id=conversation_id, user_text=user_text, kw=kw)
        from aecom.chat import ChatTurn

        return ChatTurn(conversation_id, 1, "ceo", "a reply", status="ok")

    monkeypatch.setattr(router.chat, "converse", fake_converse)
    replies = router.handle_inbound(_inbound("how's business?"))
    assert replies[0].text == "a reply"
    assert seen["user_text"] == "how's business?"
    assert seen["kw"]["paired_channel"] is True


# --------------------------------------------------------------------------- /stop (confirmation)


def test_stop_asks_for_confirmation_then_pauses_on_confirm_text():
    _pair()
    replies = router.handle_inbound(_inbound("/stop"))
    assert "Pause all agents" in replies[0].text and replies[0].buttons
    assert company_settings.agents_paused_on() is False
    router.handle_inbound(_inbound("/stop confirm"))
    assert company_settings.agents_paused_on() is True


def test_stop_confirm_button_callback_pauses_agents():
    _pair()
    router.handle_inbound(_inbound("/stop"))
    replies = router.handle_inbound(_inbound(callback_data="stop:confirm"))
    assert "paused" in replies[0].text.lower()
    assert company_settings.agents_paused_on() is True


def test_stop_cancel_button_callback_does_not_pause():
    _pair()
    router.handle_inbound(_inbound("/stop"))
    replies = router.handle_inbound(_inbound(callback_data="stop:cancel"))
    assert "Cancelled" in replies[0].text
    assert company_settings.agents_paused_on() is False


# --------------------------------------------------------------------------- approvals + tier cap


def test_approvals_command_lists_only_tiers_the_link_may_act_on():
    _pair()
    t1 = _approval("T1")
    _approval("T3")  # above the default T1 cap: not offered
    _approval("T4")  # never offered, even if the column said otherwise
    replies = router.handle_inbound(_inbound("/approvals"))
    ids = [r.text.split()[0] for r in replies]
    assert ids == [t1]
    assert replies[0].buttons and {b.data for b in replies[0].buttons} == {f"approve:{t1}", f"reject:{t1}"}


def test_approvals_command_empty_when_nothing_in_range():
    _pair()
    _approval("T3")
    replies = router.handle_inbound(_inbound("/approvals"))
    assert "No approvals" in replies[0].text


def test_approve_button_executes_and_reject_button_rejects(monkeypatch):
    from aecom.tools.catalog import CATALOG, ToolDef

    calls: list[dict] = []
    monkeypatch.setitem(CATALOG, "fake_tool", ToolDef("fake_tool", lambda **kw: calls.append(kw) or {"ok": True},
                                                       "test", True, False))
    _pair()
    apr_id = _approval("T1")
    replies = router.handle_inbound(_inbound(callback_data=f"approve:{apr_id}"))
    assert "Approved" in replies[0].text and calls == [{"x": 1}]

    apr_id2 = _approval("T1")
    replies2 = router.handle_inbound(_inbound(callback_data=f"reject:{apr_id2}"))
    assert "Rejected" in replies2[0].text
    with session() as s:
        assert s.get(Approval, apr_id2).status == "rejected"


def test_approve_button_refused_above_the_links_tier_cap():
    _pair()
    apr_id = _approval("T3")
    replies = router.handle_inbound(_inbound(callback_data=f"approve:{apr_id}"))
    assert "cannot act on T3" in replies[0].text
    with session() as s:
        assert s.get(Approval, apr_id).status == "pending"


def test_t4_is_never_approvable_even_if_the_link_cap_is_raised():
    _pair()
    with session() as s:
        link = s.query(ChannelLink).filter_by(channel="telegram", external_chat_id="42").one()
        link.max_approve_tier = "T4"
    apr_id = _approval("T4")
    replies = router.handle_inbound(_inbound(callback_data=f"approve:{apr_id}"))
    assert "cannot act on T4" in replies[0].text
    with session() as s:
        assert s.get(Approval, apr_id).status == "pending"


def test_raising_the_link_cap_to_t3_allows_t3_approvals():
    _pair()
    with session() as s:
        link = s.query(ChannelLink).filter_by(channel="telegram", external_chat_id="42").one()
        link.max_approve_tier = "T3"
    apr_id = _approval("T3")
    replies = router.handle_inbound(_inbound("/approvals"))
    assert replies and replies[0].text.split()[0] == apr_id


# --------------------------------------------------------------------------- active_links (used by alerts.py)


def test_active_links_lists_every_active_paired_chat_for_a_channel():
    _pair("1")
    _pair("2")
    with session() as s:
        s.add(ChannelLink(channel="telegram", external_chat_id="3", paired_by="x", active=False))
    ids = sorted(link.external_chat_id for link in router.active_links("telegram"))
    assert ids == ["1", "2"]
