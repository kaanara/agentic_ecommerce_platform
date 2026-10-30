"""Per-secret connectivity checks: the "Test" buttons next to secret fields on the dashboard Settings page.

One function per secret-backed integration (Anthropic, AgentMail, Lemon Squeezy, ntfy, healthchecks.io, Telegram),
each doing exactly one cheap, read-only (or, for healthchecks, intentionally side-effect-free in the sense that a
"test" check-in IS the product) call through ``aecom.net_guard`` - the same IP-pinned, SSRF-guarded GET that
``channels/telegram.py`` and ``channels/whatsapp.py`` use - never plain ``httpx``.

A secret must never reach a log, a toast or ``ops_errors``: on failure, every secret value involved in the call is
stripped from the exception text before the generic ``observability.mask`` runs on what is left (``mask`` only
catches ``*_KEY``/``*_TOKEN``/``*_SECRET``/``*_DSN``-suffixed values by itself, so URL-shaped secrets such as the
ntfy topic or the healthchecks ping id must be passed in explicitly - see ``aecom.alerts._failed`` for the same
pattern). Every function returns ``(ok, message)``, never raises.

``PINGS`` maps a ``settings_registry`` secret key (lower snake_case) to its ping function, for the dashboard to look
one up without a long if/elif; a secret with no sensible outbound check (the webhook HMAC secrets) is absent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from aecom.config import get_settings
from aecom.net_guard import pinned_get, resolve_public
from aecom.observability import mask, record_error
from aecom.secrets import secrets

log = logging.getLogger(__name__)

HTTP_TIMEOUT_S = 15
ANTHROPIC_VERSION = "2023-06-01"  # kept in sync with provisioning.verify_anthropic
JSONAPI = "application/vnd.api+json"


def _scrub(text: str, *secret_values: str | None) -> str:
    """``text`` with every given secret value replaced first, then the generic ``mask()`` as a second line of
    defense for anything it recognises by shape or env-var naming convention."""
    for value in secret_values:
        if value:
            text = text.replace(value, "***")
    return mask(text)


def _get(url: str, headers: dict[str, str] | None = None) -> httpx.Response:
    """One IP-pinned GET through ``net_guard`` (see module docstring); raises on any non-2xx or network failure."""
    target = resolve_public(url)
    response = pinned_get(target, timeout=HTTP_TIMEOUT_S, headers=headers)
    response.raise_for_status()
    return response


def _ping(name: str, call: Callable[[], httpx.Response], *secret_values: str | None) -> tuple[bool, str]:
    """Run ``call``; ``(True, "<name> connection OK.")`` or ``(False, "<name>: <masked reason>")``. Never raises -
    a failed test must report back to the owner, not crash the Settings page."""
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - every failure becomes a short, secret-free result, not a crash
        detail = _scrub(f"{type(exc).__name__}: {exc}", *secret_values)
        log.warning("%s test failed: %s", name, detail)
        record_error(f"ping.{name}", RuntimeError(detail), context={"check": name})
        return False, f"{name}: {detail}"
    return True, f"{name} connection OK."


def ping_anthropic_api_key() -> tuple[bool, str]:
    """GET ``/v1/models``: the cheapest authenticated Anthropic call (lists model ids, nothing account-specific)."""
    key = secrets.get("ANTHROPIC_API_KEY")
    if not key:
        return False, "Anthropic API key is not set."
    call = lambda: _get("https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
    return _ping("Anthropic", call, key)


def ping_agentmail_api_key() -> tuple[bool, str]:
    """GET ``/inboxes``: the least invasive authenticated call, lists the inboxes the key can see."""
    key = secrets.get("AGENTMAIL_API_KEY")
    if not key:
        return False, "AgentMail API key is not set."
    base = get_settings().agentmail_base_url.rstrip("/")
    call = lambda: _get(f"{base}/inboxes", {"Authorization": f"Bearer {key}"})
    return _ping("AgentMail", call, key)


def ping_lemonsqueezy_api_key() -> tuple[bool, str]:
    """GET ``/users/me``: Lemon Squeezy's standard "who am I" call, same one ``provisioning.verify_lemonsqueezy`` uses."""
    key = secrets.get("LEMONSQUEEZY_API_KEY")
    if not key:
        return False, "Lemon Squeezy API key is not set."
    base = get_settings().lemonsqueezy_base_url.rstrip("/")
    call = lambda: _get(f"{base}/users/me", {"Authorization": f"Bearer {key}", "Accept": JSONAPI})
    return _ping("Lemon Squeezy", call, key)


def ping_ntfy_url() -> tuple[bool, str]:
    """GET ``<topic>?poll=1&since=5m``: ntfy's non-destructive read of recent messages - proves the topic URL (and,
    if set, the access token) actually work without publishing a test notification. Shared by both ntfy secrets
    (``ntfy_url`` and ``ntfy_token``): neither is useful to test alone."""
    url = secrets.get("NTFY_URL")
    if not url:
        return False, "ntfy topic URL is not set."
    token = secrets.get("NTFY_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    poll_url = f"{url.rstrip('/')}?poll=1&since=5m"
    call = lambda: _get(poll_url, headers)
    return _ping("ntfy", call, url, token)


def ping_healthchecks_ping_url() -> tuple[bool, str]:
    """GET the ping URL itself: on healthchecks.io (and compatible self-hosted servers) a bare GET *is* the
    heartbeat - the same request ``aecom.alerts.heartbeat`` sends on schedule - so testing it also legitimately
    checks in the monitor, which is the intended, harmless behaviour."""
    url = secrets.get("HEALTHCHECKS_PING_URL")
    if not url:
        return False, "healthchecks.io ping URL is not set."
    call = lambda: _get(url)
    return _ping("healthchecks.io", call, url)


def ping_telegram_bot_token() -> tuple[bool, str]:
    """``getMe``: confirms the token is valid; on success the message names the bot's own (public) username."""
    token = secrets.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "Telegram bot token is not set."
    bot: dict[str, Any] = {}

    def call() -> httpx.Response:
        response = _get(f"https://api.telegram.org/bot{token}/getMe")
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description") or "Telegram rejected the request.")
        bot.update(body.get("result") or {})
        return response

    ok, message = _ping("Telegram", call, token)
    if ok and bot.get("username"):
        return True, f"Telegram bot @{bot['username']} is reachable."
    return ok, message


PINGS: dict[str, Callable[[], tuple[bool, str]]] = {
    "anthropic_api_key": ping_anthropic_api_key,
    "agentmail_api_key": ping_agentmail_api_key,
    "lemonsqueezy_api_key": ping_lemonsqueezy_api_key,
    "ntfy_url": ping_ntfy_url,
    "ntfy_token": ping_ntfy_url,
    "healthchecks_ping_url": ping_healthchecks_ping_url,
    "telegram_bot_token": ping_telegram_bot_token,
}
