"""Telegram Bot API client: getUpdates long polling, sendMessage, callback acknowledgement - all via net_guard.

Offline only: `net_guard.resolve_public`/`pinned_get` are monkeypatched so no socket is ever touched, and
`aecom.channels.telegram.handle_inbound` is monkeypatched so these tests stay about the Telegram plumbing
(parsing updates, building requests, tracking the update offset) rather than the router (see test_channels.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from aecom import company_settings
from aecom.channels import telegram
from aecom.channels.base import Button, Inbound, Reply
from aecom.config import get_settings

FIX = Path(__file__).parent / "fixtures" / "telegram"


def _load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeTelegramApi:
    """Records every call; `responses[method]` is returned (defaults to `{"ok": True, "result": {}}`)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: dict[str, dict] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telegram, "resolve_public", lambda url: url)
        monkeypatch.setattr(telegram, "pinned_get", self._pinned_get)

    def _pinned_get(self, target: str, timeout: float = 15) -> httpx.Response:
        parts = urlsplit(target)
        method = parts.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append({"method": method, "params": params})
        body = self.responses.get(method, {"ok": True, "result": {}})
        return httpx.Response(200, json=body, request=httpx.Request("GET", target))


@pytest.fixture
def api(monkeypatch) -> FakeTelegramApi:
    fake = FakeTelegramApi()
    fake.install(monkeypatch)
    monkeypatch.setattr(telegram.secrets, "get", lambda name: "test-token" if name == "TELEGRAM_BOT_TOKEN" else None)
    monkeypatch.setattr(telegram.secrets, "has", lambda name: name == "TELEGRAM_BOT_TOKEN")
    get_settings().telegram_enabled = True
    return fake


# --------------------------------------------------------------------------- enabled()


def test_disabled_without_the_setting_or_the_token(monkeypatch):
    monkeypatch.setattr(telegram.secrets, "has", lambda name: False)
    get_settings().telegram_enabled = False
    assert telegram.TelegramChannel().enabled() is False


def test_enabled_needs_both_the_setting_and_the_token(api):
    assert telegram.TelegramChannel().enabled() is True
    get_settings().telegram_enabled = False
    assert telegram.TelegramChannel().enabled() is False


# --------------------------------------------------------------------------- send()


def test_send_posts_chat_id_and_text(api):
    assert telegram.TelegramChannel().send("42", "hello owner") is True
    call = api.calls[-1]
    assert call["method"] == "sendMessage"
    assert call["params"]["chat_id"] == "42" and call["params"]["text"] == "hello owner"
    assert "reply_markup" not in call["params"]


def test_send_with_buttons_encodes_an_inline_keyboard(api):
    buttons = [Button("Approve", "approve:appr_1"), Button("Reject", "reject:appr_1")]
    assert telegram.TelegramChannel().send("42", "approve this?", buttons) is True
    markup = json.loads(api.calls[-1]["params"]["reply_markup"])
    assert markup["inline_keyboard"] == [[{"text": "Approve", "callback_data": "approve:appr_1"}],
                                         [{"text": "Reject", "callback_data": "reject:appr_1"}]]


def test_send_returns_false_when_telegram_rejects_the_call(api):
    api.responses["sendMessage"] = {"ok": False, "description": "chat not found"}
    assert telegram.TelegramChannel().send("42", "hi") is False


def test_send_never_raises_when_the_network_fails(api, monkeypatch):
    def boom(target, timeout=15):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(telegram, "pinned_get", boom)
    assert telegram.TelegramChannel().send("42", "hi") is False


def test_answer_callback_acknowledges_by_id(api):
    assert telegram.TelegramChannel().answer_callback("cbq-1") is True
    assert api.calls[-1] == {"method": "answerCallbackQuery", "params": {"callback_query_id": "cbq-1"}}


def test_answer_callback_is_a_noop_without_an_id(api):
    assert telegram.TelegramChannel().answer_callback("") is False
    assert api.calls == []


# --------------------------------------------------------------------------- parse_inbound()


def test_parse_inbound_message_update():
    update = _load("get_updates_message")["result"][0]
    msg = telegram.TelegramChannel().parse_inbound(update)
    assert msg == Inbound(channel="telegram", external_chat_id="555000111", text="/pair 012345",
                          message_id="5001")


def test_parse_inbound_callback_query_update():
    update = _load("get_updates_callback")["result"][0]
    msg = telegram.TelegramChannel().parse_inbound(update)
    assert msg.channel == "telegram" and msg.external_chat_id == "555000111"
    assert msg.callback_data == "approve:appr_1" and msg.callback_id == "cbq-1"
    assert msg.text == ""


def test_parse_inbound_ignores_updates_with_neither_message_nor_callback():
    assert telegram.TelegramChannel().parse_inbound({"update_id": 1, "edited_message": {}}) is None


# --------------------------------------------------------------------------- poll_updates()


def test_poll_updates_is_a_noop_when_the_channel_is_disabled(monkeypatch):
    get_settings().telegram_enabled = False
    assert telegram.poll_updates() == 0


def test_poll_updates_processes_each_update_sends_replies_and_acks_callbacks(api, monkeypatch):
    api.responses["getUpdates"] = _load("get_updates_callback")
    monkeypatch.setattr(telegram, "handle_inbound", lambda msg: [Reply("done", None)])

    assert telegram.poll_updates() == 1
    methods = [c["method"] for c in api.calls]
    assert methods == ["getUpdates", "answerCallbackQuery", "sendMessage"]
    assert api.calls[-1]["params"] == {"chat_id": "555000111", "text": "done"}


def test_poll_updates_advances_and_persists_the_offset(api, monkeypatch):
    api.responses["getUpdates"] = _load("get_updates_message")
    monkeypatch.setattr(telegram, "handle_inbound", lambda msg: [])

    telegram.poll_updates()
    assert company_settings.get_setting(telegram.OFFSET_KEY) == "100001"
    # the next poll must ask for updates strictly after the one already processed
    telegram.poll_updates()
    assert api.calls[-1]["params"]["offset"] == "100002"


def test_poll_updates_with_nothing_waiting_returns_zero(api):
    api.responses["getUpdates"] = _load("get_updates_empty")
    assert telegram.poll_updates() == 0


def test_poll_updates_never_raises_when_the_router_blows_up(api, monkeypatch):
    api.responses["getUpdates"] = _load("get_updates_message")

    def broken(msg):
        raise RuntimeError("router bug")

    monkeypatch.setattr(telegram, "handle_inbound", broken)
    assert telegram.poll_updates() == 1  # the update still counts as processed (offset must still advance)
