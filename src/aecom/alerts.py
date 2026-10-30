"""B/L2: alert routing for incidents, plus the external heartbeat. Plain HTTP, never an LLM.

| Sev | Channel |
|---|---|
| P1 | Telegram (every paired chat) + phone push via ntfy (``NTFY_URL``, optional ``NTFY_TOKEN``) + email to ``ALERT_EMAIL`` |
| P2 | Telegram (every paired chat) + email to ``ALERT_EMAIL`` |
| P3 | none here (daily digest, planned) |

Email goes through AgentMail, so it stays in the local outbox unless ``AGENTMAIL_LIVE=true``. ``heartbeat`` GETs
``HEALTHCHECKS_PING_URL`` (a dead-man's switch such as healthchecks.io). Every channel is a no-op without its
config; network errors are logged and recorded in ops_errors but never raise. Alert text is secret-masked.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from aecom.integrations.agentmail import AgentMail
from aecom.observability import mask, record_error
from aecom.secrets import secrets

log = logging.getLogger(__name__)

TIMEOUT = 10
PUSH_SEVERITIES = ("P1",)
EMAIL_SEVERITIES = ("P1", "P2")
TELEGRAM_SEVERITIES = ("P1", "P2")


def _failed(channel: str, exc: BaseException, url: str, **context: Any) -> None:
    """Log and record a channel failure without the URL (ntfy topics and ping URLs are secrets in themselves)."""
    detail = mask(str(exc)).replace(url, "<url>")
    log.warning("%s alert channel failed: %s: %s", channel, type(exc).__name__, detail)
    record_error("alerts", RuntimeError(f"{channel} failed: {type(exc).__name__}: {detail}"),
                 context={"channel": channel, **context})


def _title(incident: dict[str, Any]) -> str:
    subject = f": {incident['subject']}" if incident.get("subject") else ""
    return mask(f"[{incident['severity']}] {incident['monitor']}{subject}")


def _body(incident: dict[str, Any]) -> str:
    lines = [incident.get("detail") or "",
             "",
             f"incident {incident['id']} - status {incident['status']}, first seen {incident['first_seen']}, "
             f"alert #{(incident.get('alert_count') or 0) + 1}",
             f"Acknowledge: uv run aecom incidents ack {incident['id']} --by <you>"]
    return mask("\n".join(lines))


def push(incident: dict[str, Any]) -> bool:
    """ntfy push (title + priority high). Returns True if sent."""
    url = secrets.get("NTFY_URL")
    if not url:
        return False
    # HTTP headers must be latin-1; ntfy titles are short, so replace anything else
    headers = {"Title": _title(incident).encode("ascii", "replace").decode(), "Priority": "high", "Tags": "rotating_light"}
    if token := secrets.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = httpx.post(url, content=_body(incident).encode(), headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as exc:
        _failed("ntfy", exc, url, incident_id=incident["id"])
        return False


def email(incident: dict[str, Any]) -> bool:
    """Alert email via AgentMail. Returns True if handed to AgentMail."""
    to = secrets.get("ALERT_EMAIL")
    if not to:
        return False
    try:
        AgentMail().send(to, _title(incident), _body(incident))
        return True
    except Exception as exc:
        log.warning("alert email for incident %s failed: %s", incident["id"], mask(str(exc)))
        record_error("alerts", exc, context={"channel": "email", "incident_id": incident["id"]})
        return False


def telegram(incident: dict[str, Any]) -> bool:
    """Send the incident to every paired Telegram chat. Returns True if at least one chat was sent to (no-op,
    returning False, when Telegram is off/unconfigured or no chat is paired)."""
    from aecom.channels.router import active_links
    from aecom.channels.telegram import TelegramChannel

    channel = TelegramChannel()
    if not channel.enabled():
        return False
    links = active_links("telegram")
    if not links:
        return False
    text = f"{_title(incident)}\n{_body(incident)}"
    sent = False
    for link in links:
        try:
            sent = channel.send(link.external_chat_id, text) or sent
        except Exception as exc:
            _failed("telegram", exc, "", incident_id=incident["id"])
    return sent


def route(incident: dict[str, Any]) -> dict[str, bool]:
    """Send the incident to the channels of its severity. Never raises."""
    sev = incident["severity"]
    return {"push": push(incident) if sev in PUSH_SEVERITIES else False,
            "email": email(incident) if sev in EMAIL_SEVERITIES else False,
            "telegram": telegram(incident) if sev in TELEGRAM_SEVERITIES else False}


def heartbeat() -> bool:
    """Ping the external dead-man's switch. Returns True if pinged."""
    url = secrets.get("HEALTHCHECKS_PING_URL")
    if not url:
        return False
    try:
        httpx.get(url, timeout=TIMEOUT).raise_for_status()
        return True
    except Exception as exc:
        _failed("heartbeat", exc, url)
        return False
