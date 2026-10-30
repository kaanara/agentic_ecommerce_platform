"""Chat retention: the nightly scheduler job and attachment-file cleanup on delete/purge."""

from datetime import timedelta

import pytest

from aecom import chat, dashboard_auth
from aecom import conversations as conv
from aecom.config import get_settings
from aecom.db import Conversation, now, session
from aecom.llm import Script


@pytest.fixture(autouse=True)
def _login_on():
    """Chat (used for the attachment-cleanup tests) requires the dashboard login to be configured."""
    dashboard_auth.set_password("a-strong-password-123", "a-strong-password-123", "tester")
    yield


def test_scheduler_registers_chat_retention_job():
    from aecom import scheduler

    jobs = {j.id: j for j in scheduler.build_scheduler(demo=False).get_jobs()}
    assert "chat_retention" in jobs
    fields = {f.name: str(f) for f in jobs["chat_retention"].trigger.fields}
    assert (fields["minute"], fields["hour"]) == ("30", "3")


def test_chat_retention_job_purges_old_conversations(monkeypatch):
    from aecom import scheduler

    old = conv.create("ceo")
    conv.append_message(old, "user", "old")
    with session() as s:
        s.get(Conversation, old).updated_at = now() - timedelta(days=200)
    monkeypatch.setattr(get_settings(), "chat_retention_days", 90)
    scheduler.chat_retention()
    assert conv.get(old) is None


def test_chat_retention_job_skips_when_zero(monkeypatch):
    from aecom import scheduler

    old = conv.create("ceo")
    conv.append_message(old, "user", "old")
    with session() as s:
        s.get(Conversation, old).updated_at = now() - timedelta(days=200)
    monkeypatch.setattr(get_settings(), "chat_retention_days", 0)
    scheduler.chat_retention()
    assert conv.get(old) is not None


def test_delete_removes_attachment_files(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "ATTACHMENT_DIR", tmp_path / "chat_files")
    cid = conv.create("finance", "alice")
    attachments = [
        {"type": "text", "name": "notes.txt", "content": "Sensitive pricing data"},
        {"type": "csv", "name": "prices.csv", "content": "SKU,Price\nPROD-1,99.99"},
    ]

    Script.load({"finance": [{"text": "reviewed the attachments"}]})
    chat.converse(cid, "analyze this pricing data", attachments=attachments)
    saved = list((tmp_path / "chat_files").glob("*"))
    assert len(saved) == 2

    assert conv.delete(cid) is True
    assert list((tmp_path / "chat_files").glob("*")) == []


def test_purge_older_than_removes_attachment_files(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "ATTACHMENT_DIR", tmp_path / "chat_files")
    cid = conv.create("finance", "alice")
    attachments = [{"type": "text", "name": "notes.txt", "content": "old pricing data"}]

    Script.load({"finance": [{"text": "reviewed"}]})
    chat.converse(cid, "look at this", attachments=attachments)
    assert len(list((tmp_path / "chat_files").glob("*"))) == 1

    with session() as s:
        s.get(Conversation, cid).updated_at = now() - timedelta(days=200)
    assert conv.purge_older_than(90) == 1
    assert list((tmp_path / "chat_files").glob("*")) == []


def test_delete_attachment_cleanup_is_path_traversal_safe(tmp_path, monkeypatch):
    """A maliciously crafted attachment name must never let delete escape ATTACHMENT_DIR."""
    attach_dir = tmp_path / "chat_files"
    attach_dir.mkdir()
    monkeypatch.setattr(chat, "ATTACHMENT_DIR", attach_dir)

    outside = tmp_path / "secret.txt"
    outside.write_text("do not delete me")

    cid = conv.create("finance", "alice")
    conv.append_message(cid, "user", "hi", attachments=[{"name": "../secret.txt", "type": "text"}])

    assert conv.delete(cid) is True
    assert outside.exists()


def test_delete_with_no_attachments_never_raises():
    cid = conv.create("ceo")
    conv.append_message(cid, "user", "hi")
    assert conv.delete(cid) is True


def test_attachment_cleanup_missing_directory_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "ATTACHMENT_DIR", tmp_path / "does_not_exist")
    cid = conv.create("ceo")
    conv.append_message(cid, "user", "hi", attachments=[{"name": "foo.txt", "type": "text"}])
    assert conv.delete(cid) is True
