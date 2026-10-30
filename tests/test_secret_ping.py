"""Secret "Test" pings (`aecom.integrations.ping`): one safe, read-only call per secret, through `net_guard`.

Offline only: `ping.resolve_public`/`pinned_get` are monkeypatched to a `FakeHTTP` that never touches a socket (real
SSRF behaviour of `net_guard` itself is covered by test_security.py). Every scenario checks that the secret value
never ends up in the returned message, whether the call succeeds or fails.
"""

from __future__ import annotations

import httpx
import pytest

from aecom.integrations import ping
from aecom.config import get_settings

ANTHROPIC_KEY = "sk-ant-test-anthropic-secret"
AGENTMAIL_KEY = "am-test-agentmail-secret"
LEMONSQUEEZY_KEY = "ls-test-lemonsqueezy-secret"
NTFY_URL = "https://ntfy.sh/aecom-test-topic-9f8e7d"
NTFY_TOKEN = "tk_test-ntfy-secret"
HEALTHCHECKS_URL = "https://hc-ping.com/11111111-2222-3333-4444-555555555555"
TELEGRAM_TOKEN = "123456:ABC-test-telegram-secret"

ALL_SECRETS = {
    "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
    "AGENTMAIL_API_KEY": AGENTMAIL_KEY,
    "LEMONSQUEEZY_API_KEY": LEMONSQUEEZY_KEY,
    "NTFY_URL": NTFY_URL,
    "NTFY_TOKEN": NTFY_TOKEN,
    "HEALTHCHECKS_PING_URL": HEALTHCHECKS_URL,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
}


class FakeHTTP:
    """Records every `pinned_get` call; `response` (or `exc`) decides what the next call gets back."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.response: httpx.Response = httpx.Response(200, json={})
        self.exc: BaseException | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ping, "resolve_public", lambda url: url)  # identity: `_get` forwards it untouched
        monkeypatch.setattr(ping, "pinned_get", self._pinned_get)

    def _pinned_get(self, target: str, *, timeout: float = 15, headers: dict | None = None) -> httpx.Response:
        self.calls.append({"url": target, "headers": headers})
        if self.exc is not None:
            raise self.exc
        self.response.request = httpx.Request("GET", target)
        return self.response


@pytest.fixture
def http(monkeypatch) -> FakeHTTP:
    fake = FakeHTTP()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def configured(monkeypatch) -> dict[str, str]:
    """Every secret the pings care about is set; a test narrows this by overriding `ping.secrets.get` further."""
    values = dict(ALL_SECRETS)
    monkeypatch.setattr(ping.secrets, "get", lambda name: values.get(name))
    return values


# --------------------------------------------------------------------------- PINGS mapping
def test_pings_mapping_matches_the_registrys_secret_fields():
    from aecom import settings_registry as reg

    secret_keys = {key for key, field in reg.FIELDS.items() if field.kind == "secret"}
    assert set(ping.PINGS) == {"anthropic_api_key", "agentmail_api_key", "lemonsqueezy_api_key", "ntfy_url",
                               "ntfy_token", "healthchecks_ping_url", "telegram_bot_token"}
    assert set(ping.PINGS) <= secret_keys  # every mapped key is a real secret
    assert "lemonsqueezy_webhook_secret" not in ping.PINGS  # webhook HMAC secrets have no sensible outbound ping
    assert "agentmail_webhook_secret" not in ping.PINGS


def test_ntfy_url_and_ntfy_token_share_the_same_ping():
    assert ping.PINGS["ntfy_url"] is ping.PINGS["ntfy_token"]


# --------------------------------------------------------------------------- missing secret: no network call
@pytest.mark.parametrize("func_name,message_word", [
    ("ping_anthropic_api_key", "Anthropic"),
    ("ping_agentmail_api_key", "AgentMail"),
    ("ping_lemonsqueezy_api_key", "Lemon Squeezy"),
    ("ping_ntfy_url", "ntfy"),
    ("ping_healthchecks_ping_url", "healthchecks"),
    ("ping_telegram_bot_token", "Telegram"),
])
def test_ping_without_a_secret_set_fails_without_touching_the_network(http, monkeypatch, func_name, message_word):
    monkeypatch.setattr(ping.secrets, "get", lambda name: None)
    ok, message = getattr(ping, func_name)()
    assert ok is False
    assert message_word.lower() in message.lower()
    assert "not set" in message
    assert not http.calls  # no resolve_public/pinned_get reached


# --------------------------------------------------------------------------- success paths
def test_anthropic_ping_succeeds_and_sends_the_api_key_header(http, configured):
    http.response = httpx.Response(200, json={"data": []})
    ok, message = ping.ping_anthropic_api_key()
    assert ok is True
    assert "Anthropic" in message
    assert ANTHROPIC_KEY not in message
    call = http.calls[0]
    assert call["headers"]["x-api-key"] == ANTHROPIC_KEY
    assert call["url"] == "https://api.anthropic.com/v1/models"


def test_agentmail_ping_succeeds_and_uses_the_configured_base_url(http, configured):
    get_settings().agentmail_base_url = "https://api.agentmail.to/v0"
    http.response = httpx.Response(200, json={"inboxes": []})
    ok, message = ping.ping_agentmail_api_key()
    assert ok is True
    assert AGENTMAIL_KEY not in message
    call = http.calls[0]
    assert call["url"] == "https://api.agentmail.to/v0/inboxes"
    assert call["headers"]["Authorization"] == f"Bearer {AGENTMAIL_KEY}"


def test_lemonsqueezy_ping_succeeds_and_uses_the_configured_base_url(http, configured):
    get_settings().lemonsqueezy_base_url = "https://api.lemonsqueezy.com/v1"
    http.response = httpx.Response(200, json={"data": {}})
    ok, message = ping.ping_lemonsqueezy_api_key()
    assert ok is True
    assert LEMONSQUEEZY_KEY not in message
    call = http.calls[0]
    assert call["url"] == "https://api.lemonsqueezy.com/v1/users/me"
    assert call["headers"]["Authorization"] == f"Bearer {LEMONSQUEEZY_KEY}"


def test_ntfy_ping_polls_the_topic_with_the_token_when_set(http, configured):
    http.response = httpx.Response(200, text="")
    ok, message = ping.ping_ntfy_url()
    assert ok is True
    assert NTFY_URL not in message and NTFY_TOKEN not in message
    call = http.calls[0]
    assert call["url"] == f"{NTFY_URL}?poll=1&since=5m"
    assert call["headers"]["Authorization"] == f"Bearer {NTFY_TOKEN}"


def test_ntfy_ping_omits_auth_header_when_no_token_is_set(http, monkeypatch, configured):
    monkeypatch.setattr(ping.secrets, "get", lambda name: NTFY_URL if name == "NTFY_URL" else None)
    http.response = httpx.Response(200, text="")
    ok, message = ping.ping_ntfy_url()
    assert ok is True
    assert http.calls[0]["headers"] is None


def test_healthchecks_ping_gets_the_ping_url_itself(http, configured):
    http.response = httpx.Response(200, text="OK")
    ok, message = ping.ping_healthchecks_ping_url()
    assert ok is True
    assert HEALTHCHECKS_URL not in message
    assert http.calls[0]["url"] == HEALTHCHECKS_URL


def test_telegram_ping_reports_the_bots_username(http, configured):
    http.response = httpx.Response(200, json={"ok": True, "result": {"username": "aecom_test_bot"}})
    ok, message = ping.ping_telegram_bot_token()
    assert ok is True
    assert message == "Telegram bot @aecom_test_bot is reachable."
    assert TELEGRAM_TOKEN not in message
    assert http.calls[0]["url"] == f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe"


# --------------------------------------------------------------------------- failures never leak the secret
def test_telegram_ping_fails_cleanly_when_telegram_rejects_the_token(http, configured):
    http.response = httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    ok, message = ping.ping_telegram_bot_token()
    assert ok is False
    assert TELEGRAM_TOKEN not in message
    assert "Telegram" in message


@pytest.mark.parametrize("func_name,key,secret_value", [
    ("ping_anthropic_api_key", "ANTHROPIC_API_KEY", ANTHROPIC_KEY),
    ("ping_agentmail_api_key", "AGENTMAIL_API_KEY", AGENTMAIL_KEY),
    ("ping_lemonsqueezy_api_key", "LEMONSQUEEZY_API_KEY", LEMONSQUEEZY_KEY),
    ("ping_healthchecks_ping_url", "HEALTHCHECKS_PING_URL", HEALTHCHECKS_URL),
    ("ping_telegram_bot_token", "TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN),
])
def test_network_failure_never_puts_the_secret_in_the_message_or_the_error_log(http, configured, func_name, key,
                                                                               secret_value):
    from sqlalchemy import select

    from aecom.db import ErrorRow, session

    http.exc = httpx.ConnectError(f"Connection refused while calling a URL containing {secret_value}")
    ok, message = getattr(ping, func_name)()
    assert ok is False
    assert secret_value not in message
    assert "***" in message
    with session() as s:
        rows = s.scalars(select(ErrorRow)).all()
    assert rows  # record_error recorded the failure
    assert all(secret_value not in (r.message or "") for r in rows)
    assert all(secret_value not in (r.traceback or "") for r in rows)


def test_ntfy_network_failure_masks_both_url_and_token(http, configured):
    http.exc = httpx.ConnectError(f"Connection refused to {NTFY_URL} with token {NTFY_TOKEN}")
    ok, message = ping.ping_ntfy_url()
    assert ok is False
    assert NTFY_URL not in message
    assert NTFY_TOKEN not in message
