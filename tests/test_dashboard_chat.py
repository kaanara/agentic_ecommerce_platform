"""AppTest smoke tests for dashboard/app_pages/chat.py (W1 builder B).

``aecom.chat.converse`` is monkeypatched here for the streaming round trip: the real implementation is built by
builder A and may still be the W0 stub while this page is tested.
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import chat, conversations
from aecom import dashboard_auth as auth
from aecom.company_settings import set_setting

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the page does `from _common import ...`

HOME = str(DASHBOARD / "Home.py")
CHAT_PAGE = "app_pages/chat.py"


def _app(login_off: bool = False) -> AppTest:
    if login_off:
        set_setting(auth.MODE_KEY, "off")
    else:
        auth.reset_password_local("a long test password")
    return AppTest.from_file(HOME, default_timeout=60)


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def _logged_in() -> AppTest:
    at = _app().run()
    at.text_input[0].set_value("a long test password")
    at.button[0].click()
    at.run()
    return at.switch_page(CHAT_PAGE).run()


def fake_converse(conversation_id, user_text, *, attachments=None, on_event=None, paired_channel=False):
    """A scripted ``converse`` that persists like the real one and emits every streaming event type."""
    conversations.append_message(conversation_id, "user", user_text, attachments=attachments)
    if on_event is not None:
        on_event({"type": "tool_start", "name": "lookup_kpi", "tool_call_id": "t1"})
        on_event({"type": "text_delta", "text": "Hello "})
        on_event({"type": "text_delta", "text": "there."})
        on_event({"type": "tool_end", "name": "lookup_kpi", "tool_call_id": "t1", "result": "42"})
    mid = conversations.append_message(conversation_id, "assistant", "Hello there.", agent_id="ceo", cost_eur=0.01,
                                       agent_run_id="chat-abc123", tool_steps=[{"name": "lookup_kpi"}])
    return chat.ChatTurn(conversation_id=conversation_id, message_id=mid, agent_id="ceo", text="Hello there.",
                         status="ok", cost_eur=0.01, tool_steps=[{"name": "lookup_kpi"}], agent_run_id="chat-abc123")


def test_chat_shows_turn_on_login_when_blocked():
    at = _app(login_off=True).run()
    at.switch_page(CHAT_PAGE).run()
    assert not at.exception, _messages(at)
    assert any("Turn on login" in m.value for m in at.markdown)


def test_empty_state_with_no_conversation():
    at = _logged_in()
    assert not at.exception, _messages(at)
    assert any("No conversation selected" in m.value for m in at.markdown)
    assert at.get("pills")  # the agent picker is in the Conversations popover
    assert at.get("segmented_control")  # the Inbox/All/Archived filter


def test_new_chat_and_streamed_reply(monkeypatch):
    monkeypatch.setattr(chat, "converse", fake_converse)
    at = _logged_in()
    new_chat = next(b for b in at.button if b.label == "New chat")
    new_chat.click()
    at.run()
    assert not at.exception, _messages(at)
    assert at.chat_input, "chat_input should appear once a conversation is selected"

    at.chat_input[0].set_value("hi there")
    at.run()
    assert not at.exception, _messages(at)
    assert any("hi there" in m.value for m in at.markdown)
    assert any("Hello there." in m.value for m in at.markdown)
    assert any("\u20ac0.0100" in c.value for c in at.caption)
    assert any("chat-abc123" in c.value for c in at.caption)
    assert any(s.label and "lookup_kpi" in s.label for s in at.status)


def test_conversation_list_shows_created_conversation(monkeypatch):
    monkeypatch.setattr(chat, "converse", fake_converse)
    cid = conversations.create("ceo", owner="alice", title="Budget check")
    at = _logged_in()
    at.get("segmented_control")[0].set_value("All")  # a fresh conversation has no unread message yet
    at.run()
    assert not at.exception, _messages(at)
    assert any(b.label and "Budget check" in b.label for b in at.button)
    conv_btn = next(b for b in at.button if b.label and "Budget check" in b.label)
    conv_btn.click()
    at.run()
    assert not at.exception, _messages(at)
    assert at.query_params.get("conversation") == cid


def test_archived_filter_hides_archived_conversation_from_inbox():
    cid = conversations.create("ceo", owner="alice", title="Old topic")
    conversations.append_message(cid, "user", "hello")
    conversations.append_message(cid, "assistant", "hi", agent_id="ceo")
    conversations.archive(cid, True)
    at = _logged_in()
    assert not any(b.label and "Old topic" in b.label for b in at.button)
    at.get("segmented_control")[0].set_value("Archived")
    at.run()
    assert not at.exception, _messages(at)
    assert any(b.label and "Old topic" in b.label for b in at.button)
