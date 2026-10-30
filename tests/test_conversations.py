"""Conversation storage: masking on write, unread counters, hard delete without cascade support, retention."""

from datetime import timedelta

from sqlalchemy import select

from aecom import conversations as conv
from aecom.db import ChatMessage, Conversation, now, session


def test_title_and_masking():
    cid = conv.create("ceo", "kaan")
    conv.append_message(cid, "user", "my key is sk-ant-api03-" + "x" * 40 + "\nsecond line")
    got = conv.get(cid)
    assert got["title"].startswith("my key is")
    assert "sk-ant-api03" not in got["messages"][0]["content"]
    assert "sk-ant-api03" not in got["title"]


def test_long_title_is_cut():
    assert len(conv.auto_title("word " * 50)) <= conv.TITLE_MAX
    assert conv.auto_title("   ") == "New chat"


def test_cost_unread_and_order():
    cid = conv.create("finance", started_by="agent")
    conv.append_message(cid, "assistant", "Revenue is down", agent_id="finance", cost_eur=0.02)
    conv.append_message(cid, "user", "why?")
    conv.append_message(cid, "assistant", "Refunds", agent_id="finance", cost_eur=0.03)
    got = conv.get(cid)
    assert [m["role"] for m in got["messages"]] == ["assistant", "user", "assistant"]
    assert abs(got["total_cost_eur"] - 0.05) < 1e-9
    assert got["unread"] == 2 and conv.unread_total() == 2
    conv.mark_read(cid)
    assert conv.unread_total() == 0


def test_list_filters():
    a, b = conv.create("ceo"), conv.create("finance")
    conv.append_message(a, "user", "pricing plan")
    conv.append_message(b, "user", "tax question")
    conv.archive(b)
    assert [c["id"] for c in conv.list_conversations()] == [a]
    assert {c["id"] for c in conv.list_conversations(include_archived=True)} == {a, b}
    assert [c["id"] for c in conv.list_conversations(search="pric")] == [a]


def test_delete_removes_messages():
    cid = conv.create("ceo")
    conv.append_message(cid, "user", "hi")
    assert conv.delete(cid, "kaan") is True
    assert conv.get(cid) is None
    with session() as s:
        assert s.scalars(select(ChatMessage)).all() == []
    assert conv.delete(cid) is False


def test_purge_older_than():
    old, new = conv.create("ceo"), conv.create("ceo")
    conv.append_message(old, "user", "old")
    conv.append_message(new, "user", "new")
    with session() as s:
        s.get(Conversation, old).updated_at = now() - timedelta(days=100)
    assert conv.purge_older_than(0) == 0
    assert conv.purge_older_than(90) == 1
    assert conv.get(old) is None and conv.get(new) is not None
