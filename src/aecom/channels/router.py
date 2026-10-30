"""Inbound messages -> paired-owner check -> command or chat turn.

Unpaired chats are ignored and audited. Commands (paired chats only): ``/pair CODE`` (also works while unpaired -
that is how pairing happens), ``/agent ID``, ``/new``, ``/status``, ``/approvals``, ``/stop`` (pause agents, with a
confirmation). Anything else is forwarded to ``chat.converse(paired_channel=True)``.

Pairing: the dashboard calls ``generate_pairing_code()`` to show a one-time code valid for ``PAIR_CODE_TTL``; the
owner sends ``/pair CODE`` to the bot and the chat id is stored in ``ops_channel_links``. Pairing is refused while
the dashboard login is off (a paired channel is otherwise an unauthenticated way to reach the agents).

Approve/reject buttons (``/approvals`` and the buttons Telegram's callback_query sends back) are capped by the
link's ``max_approve_tier``: T4 is never offered regardless of what is stored, T3 is off unless the link's cap is
raised to T3 (the column defaults to T1).
"""

from __future__ import annotations

import json
import secrets as pysecrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from aecom import chat, company_settings, dashboard_auth, incidents
from aecom import conversations as conv
from aecom.audit import audit
from aecom.channels.base import Button, Inbound, Reply
from aecom.db import ChannelLink, now, session
from aecom.policy.gate import TIER_ORDER

PAIR_CODE_KEY = "telegram_pair_code"
PAIR_CODE_TTL = timedelta(minutes=10)
SESSION_KEY_PREFIX = "telegram_session:"
DEFAULT_AGENT = "ceo"
# T4 ("accounts, logins and form submission are human-only") is never approvable from a phone, no matter what a
# link's max_approve_tier column says.
NEVER_APPROVABLE = "T4"


# --------------------------------------------------------------------------- pairing codes


def generate_pairing_code(by: str = "owner") -> str:
    """A fresh 6-digit code, valid for ``PAIR_CODE_TTL``, replacing any code issued earlier. Dashboard-facing."""
    if dashboard_auth.disabled():
        raise PermissionError("pairing needs the dashboard login to be turned on first")
    code = f"{pysecrets.randbelow(1_000_000):06d}"
    expires_at = now() + PAIR_CODE_TTL
    company_settings.set_setting(PAIR_CODE_KEY, json.dumps({"code": code, "expires_at": expires_at.isoformat()}))
    audit(f"human:{by}" if by else "system", "channel_pair_code", "issued", {"channel": "telegram"})
    return code


def _consume_pairing_code(code: str) -> bool:
    """True and consumes the stored code if ``code`` matches and has not expired. One-time use either way."""
    raw = company_settings.get_setting(PAIR_CODE_KEY)
    if not raw:
        return False
    try:
        data = json.loads(raw)
        expires_at = datetime.fromisoformat(data["expires_at"])
    except (ValueError, KeyError, TypeError):
        return False
    ok = data.get("code") == code.strip() and now() <= expires_at
    company_settings.set_setting(PAIR_CODE_KEY, "")  # one-time: gone whether it matched or expired
    return ok


# --------------------------------------------------------------------------- channel links (pairing state)


def _find_link(channel: str, external_chat_id: str) -> ChannelLink | None:
    with session() as s:
        link = s.scalar(select(ChannelLink).where(ChannelLink.channel == channel,
                                                   ChannelLink.external_chat_id == external_chat_id,
                                                   ChannelLink.active.is_(True)))
        if link is not None:
            s.expunge(link)
        return link


def active_links(channel: str) -> list[ChannelLink]:
    """Every active paired chat for `channel` (e.g. "telegram"), used by alerts.py to fan out P1/P2 incidents."""
    with session() as s:
        links = list(s.scalars(select(ChannelLink).where(ChannelLink.channel == channel,
                                                          ChannelLink.active.is_(True))))
        s.expunge_all()
        return links


def _create_link(channel: str, external_chat_id: str, by: str = "owner") -> None:
    with session() as s:
        existing = s.scalar(select(ChannelLink).where(ChannelLink.channel == channel,
                                                       ChannelLink.external_chat_id == external_chat_id))
        if existing:
            existing.active = True
        else:
            s.add(ChannelLink(channel=channel, external_chat_id=external_chat_id, paired_by=by))


# --------------------------------------------------------------------------- per-chat session (current agent/chat)


def _session_get(channel: str, external_chat_id: str) -> dict[str, Any]:
    raw = company_settings.get_setting(f"{SESSION_KEY_PREFIX}{channel}:{external_chat_id}")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _session_set(channel: str, external_chat_id: str, data: dict[str, Any]) -> None:
    company_settings.set_setting(f"{SESSION_KEY_PREFIX}{channel}:{external_chat_id}", json.dumps(data))


def _start_conversation(channel: str, external_chat_id: str, agent_id: str) -> str:
    conversation_id = conv.create(agent_id, owner=external_chat_id, channel=channel, started_by="owner")
    _session_set(channel, external_chat_id, {"agent_id": agent_id, "conversation_id": conversation_id})
    return conversation_id


# --------------------------------------------------------------------------- approvals (tier cap)


def _tier_allowed(max_tier: str, tier: str) -> bool:
    if tier == NEVER_APPROVABLE or tier not in TIER_ORDER:
        return False
    if max_tier not in TIER_ORDER:
        max_tier = "T1"
    allowed_ceiling = min(TIER_ORDER.index(max_tier), TIER_ORDER.index(NEVER_APPROVABLE) - 1)
    return TIER_ORDER.index(tier) <= allowed_ceiling


def _approvals_for_link(link: ChannelLink) -> list[dict[str, Any]]:
    from aecom import approvals

    return [a for a in approvals.list_approvals("pending") if _tier_allowed(link.max_approve_tier, a["risk_tier"])]


def _approval_reply(a: dict[str, Any]) -> Reply:
    text = f"{a['id']} [{a['risk_tier']}] {a['tool_name']} for {a['agent_id']}\n{a['reason']}"
    buttons = [Button("Approve", f"approve:{a['id']}"), Button("Reject", f"reject:{a['id']}")]
    return Reply(text, buttons)


def _handle_approval_decision(link: ChannelLink, chat_id: str, approval_id: str, approve: bool) -> list[Reply]:
    from aecom import approvals

    pending = {a["id"]: a for a in approvals.list_approvals(None)}
    a = pending.get(approval_id)
    if a is None:
        return [Reply("That approval no longer exists.")]
    if not _tier_allowed(link.max_approve_tier, a["risk_tier"]):
        audit("system", "channel_approval", "denied_tier", {"channel": "telegram", "approval_id": approval_id,
                                                             "tier": a["risk_tier"]})
        return [Reply(f"This chat cannot act on {a['risk_tier']} approvals.")]
    by = f"telegram:{chat_id}"
    try:
        if approve:
            result = approvals.approve(approval_id, by=by)
            return [Reply(f"Approved {approval_id}: {result['status']}.")]
        approvals.reject(approval_id, by=by)
        return [Reply(f"Rejected {approval_id}.")]
    except ValueError as exc:
        return [Reply(str(exc))]


# --------------------------------------------------------------------------- commands


def _status_text() -> str:
    counts = incidents.open_counts()
    from aecom import approvals

    pending = len(approvals.list_approvals("pending"))
    bits = [f"kill switch: {'ON' if company_settings.kill_switch_on() else 'off'}",
            f"agents paused: {'yes' if company_settings.agents_paused_on() else 'no'}",
            f"safe mode: {'ON' if company_settings.safe_mode_on() else 'off'}",
            f"open incidents: {sum(counts.values()) if counts else 0}",
            f"pending approvals: {pending}"]
    return "\n".join(bits)


def _handle_command(link: ChannelLink, msg: Inbound) -> list[Reply] | None:
    """A reply if `msg.text` is a recognised command, else None (the caller falls back to chat)."""
    parts = msg.text.strip().split(maxsplit=1)
    if not parts:
        return None
    cmd, rest = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")

    if cmd == "/pair":
        return [Reply("This chat is already paired.")]

    if cmd == "/agent":
        agents = chat.chat_agents()
        agent_id = rest.lower()
        if not agent_id or agent_id not in agents:
            return [Reply(f"Usage: /agent <id>. Available: {', '.join(agents)}")]
        _start_conversation(msg.channel, msg.external_chat_id, agent_id)
        return [Reply(f"Switched to {agent_id}. New chat started.")]

    if cmd == "/new":
        current = _session_get(msg.channel, msg.external_chat_id)
        agent_id = current.get("agent_id") or (chat.chat_agents() or [DEFAULT_AGENT])[0]
        _start_conversation(msg.channel, msg.external_chat_id, agent_id)
        return [Reply(f"New chat started with {agent_id}.")]

    if cmd == "/status":
        return [Reply(_status_text())]

    if cmd == "/approvals":
        pending = _approvals_for_link(link)
        if not pending:
            return [Reply("No approvals waiting that this chat can act on.")]
        return [_approval_reply(a) for a in pending]

    if cmd == "/stop":
        if rest.lower() == "confirm":
            company_settings.set_flag("agents_paused", True, by=f"telegram:{msg.external_chat_id}")
            return [Reply("Agents paused.")]
        return [Reply("Pause all agents?", [Button("Confirm", "stop:confirm"), Button("Cancel", "stop:cancel")])]

    return None


def _handle_callback(link: ChannelLink, msg: Inbound) -> list[Reply]:
    data = msg.callback_data
    if data == "stop:confirm":
        company_settings.set_flag("agents_paused", True, by=f"telegram:{msg.external_chat_id}")
        return [Reply("Agents paused.")]
    if data == "stop:cancel":
        return [Reply("Cancelled.")]
    if data.startswith("approve:"):
        return _handle_approval_decision(link, msg.external_chat_id, data.removeprefix("approve:"), approve=True)
    if data.startswith("reject:"):
        return _handle_approval_decision(link, msg.external_chat_id, data.removeprefix("reject:"), approve=False)
    return [Reply("Unknown action.")]


def handle_inbound(msg: Inbound) -> list[Reply]:
    """Inbound message -> paired-owner check -> command or chat turn. Every inbound message is audited."""
    audit("system", "channel_inbound", "received",
          {"channel": msg.channel, "external_chat_id": msg.external_chat_id,
           "kind": "callback" if msg.callback_data else "message"})

    link = _find_link(msg.channel, msg.external_chat_id)

    if link is None:
        text = msg.text.strip()
        if text.lower().startswith("/pair"):
            code = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if dashboard_auth.disabled():
                audit("system", "channel_pair", "refused_login_off", {"channel": msg.channel})
                return [Reply("Pairing needs the dashboard login turned on first (Settings > Security).")]
            if not code or not _consume_pairing_code(code):
                audit("system", "channel_pair", "denied",
                      {"channel": msg.channel, "external_chat_id": msg.external_chat_id})
                return [Reply("That code is invalid or expired. Get a new one from Settings.")]
            _create_link(msg.channel, msg.external_chat_id)
            audit("system", "channel_pair", "ok", {"channel": msg.channel, "external_chat_id": msg.external_chat_id})
            return [Reply("Paired. Send /agent ceo to start, or just say hello.")]
        audit("system", "channel_inbound_unpaired", "ignored",
              {"channel": msg.channel, "external_chat_id": msg.external_chat_id})
        return []

    if msg.callback_data:
        return _handle_callback(link, msg)

    if (reply := _handle_command(link, msg)) is not None:
        return reply

    current = _session_get(msg.channel, msg.external_chat_id)
    conversation_id = current.get("conversation_id")
    if not conversation_id or conv.get(conversation_id) is None:
        agent_id = current.get("agent_id") or (chat.chat_agents() or [DEFAULT_AGENT])[0]
        conversation_id = _start_conversation(msg.channel, msg.external_chat_id, agent_id)

    turn = chat.converse(conversation_id, msg.text, paired_channel=True)
    return [Reply(turn.text)]
