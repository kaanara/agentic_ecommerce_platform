"""Chat with agents (STUB: interface fixed in W0, built in W1 builder A).

``converse`` is the single entry point for the dashboard, the API, Telegram, WhatsApp and the CLI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aecom import dashboard_auth
from aecom.config import get_settings


@dataclass
class ChatTurn:
    conversation_id: str
    message_id: int
    agent_id: str
    text: str  # masked reply
    status: str  # ok | error | budget_exceeded | max_steps | disabled | blocked
    cost_eur: float = 0.0
    tool_steps: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)  # approval ids waiting for the owner
    agent_run_id: str | None = None


def chat_agents() -> list[str]:
    return list(get_settings().chat_agents)


def blocked_reason(*, paired_channel: bool = False) -> str | None:
    """Why chat must refuse right now, or None. Agents use real tools through the gate, so the dashboard login has to
    be on; a paired messaging channel is its own authentication."""
    if paired_channel:
        return None
    if dashboard_auth.disabled():
        return "Chat needs the dashboard login to be on. Turn it on in Settings > Security."
    if not dashboard_auth.is_configured():
        return "Chat needs a dashboard password. Set one in Settings > Security."
    return None


def converse(conversation_id: str, user_text: str, *, attachments: list[dict[str, Any]] | None = None,
             on_event: Callable[[dict[str, Any]], None] | None = None, paired_channel: bool = False) -> ChatTurn:
    """Run one turn. ``on_event`` gets ``{"type": "text_delta"|"tool_start"|"tool_end"|"escalation", ...}`` dicts."""
    raise NotImplementedError("chat.converse is built in wave 1 (builder A)")
