"""Chat with agents: the single entry point for the dashboard, the API, Telegram, WhatsApp and the CLI.

``converse`` is a plain (synchronous) function — callers never need an event loop of their own — that bridges
into the async agent run core (``aecom.graph.agents._run_core``) via ``aecom.handlers.run_sync``.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from aecom import conversations as conv
from aecom import dashboard_auth
from aecom.config import get_settings
from aecom.context import current_tainted
from aecom.db import AgentRun, new_id, now, session
from aecom.graph.agents import _run_core, daily_budget, spent_today
from aecom.handlers import run_sync
from aecom.llm import get_model
from aecom.observability import mask
from aecom.tools.catalog import fence

ATTACHMENT_DIR = Path("data/chat_files")


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


def _extract_text(msg: AIMessage) -> str:
    if isinstance(msg.content, str):
        return msg.content
    return "\n".join(b.get("text", "") for b in msg.content if isinstance(b, dict) and b.get("type") == "text")


def _tool_result_brief(step: dict[str, Any]) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in (step.get("args") or {}).items())
    result = str(step.get("result", ""))[:500]
    return f"{step.get('name')}({args}) -> {result}"[:500]


def _build_history(record: dict[str, Any]) -> list[BaseMessage]:
    """Last ``chat_history_turns`` turns, plus the folded summary, with past tool results cut to 500 characters."""
    n_turns = max(0, get_settings().chat_history_turns)
    msgs = record["messages"]
    window = msgs[-(n_turns * 2):] if n_turns else []
    history: list[BaseMessage] = []
    if record.get("summary"):
        history.append(AIMessage(content=f"(Summary of earlier conversation)\n{record['summary']}"))
    for m in window:
        content = m["content"]
        if m["role"] == "assistant" and m.get("tool_steps"):
            briefs = "; ".join(_tool_result_brief(s) for s in m["tool_steps"])
            content = f"{content}\n[earlier tool calls: {briefs}]"
        history.append(HumanMessage(content=content) if m["role"] == "user" else AIMessage(content=content))
    return history


def _summarize(agent_id: str, conversation_id: str, text: str) -> str:
    """Fold old turns into a short summary with the cheap model; its (tiny) cost is booked to the agent's chat spend."""
    settings = get_settings()
    model = get_model("haiku", f"{agent_id}:chat-summary")
    messages: list[BaseMessage] = [
        SystemMessage(content="Summarize this chat concisely for later context: preserve names, numbers and decisions."),
        HumanMessage(content=text[:8000]),
    ]
    ai = model.invoke(messages)
    usage = ai.usage_metadata or {}
    tin, tout = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    # The scripted model has no understanding of the text; fall back to a raw excerpt so no context is lost offline.
    out = text[-1500:] if settings.llm_mode == "scripted" else (_extract_text(ai).strip() or text[-1500:])
    cost = settings.cost_eur("haiku", tin, tout)
    with session() as s:
        s.add(AgentRun(run_id=f"chat-summary-{new_id('run')}", agent_id=agent_id,
                       model="scripted" if settings.llm_mode == "scripted" else settings.model_for_tier("haiku"),
                       task=f"chat summary ({conversation_id})"[:4000], output=out[:8000], status="ok", steps=1,
                       tool_calls=0, input_tokens=tin, output_tokens=tout, cost_eur=cost, started_at=now(), ended_at=now()))
    return out


def _maybe_summarize(agent_id: str, conversation_id: str, record: dict[str, Any]) -> None:
    n_turns = max(0, get_settings().chat_history_turns)
    msgs = record["messages"]
    overflow = msgs[: max(0, len(msgs) - n_turns * 2)] if n_turns else []
    if len(overflow) < 2:
        return
    text = "\n".join(f"{m['role']}: {m['content']}" for m in overflow)
    if record.get("summary"):
        text = f"{record['summary']}\n{text}"
    conv.set_summary(conversation_id, _summarize(agent_id, conversation_id, text))


def _attachment_block(attachments: list[dict[str, Any]] | None) -> str:
    """Save attachments under ``data/chat_files/`` (size-capped) and fence them into the task text; always untrusted."""
    if not attachments:
        return ""
    settings = get_settings()
    max_bytes = settings.chat_max_attachment_mb * 1024 * 1024
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    for att in attachments:
        name = str(att.get("name") or "attachment")
        kind = str(att.get("type") or "text")
        if kind == "image" and att.get("data"):
            try:
                blob = base64.b64decode(att["data"])
            except Exception:
                blob = b""
            if len(blob) > max_bytes or not blob:
                parts.append(fence(f"attachment:{name}",
                                   f"[image '{name}' skipped: missing or exceeds {settings.chat_max_attachment_mb} MB]"))
                continue
            path = ATTACHMENT_DIR / f"{new_id('att')}_{name}"
            path.write_bytes(blob)
            parts.append(fence(f"attachment:{name}", f"[image attached at {path}; not shown as text]"))
            continue
        text = att.get("content") if isinstance(att.get("content"), str) else ""
        if len(text.encode()) > max_bytes:
            parts.append(fence(f"attachment:{name}",
                               f"[{kind} '{name}' skipped: exceeds {settings.chat_max_attachment_mb} MB]"))
            continue
        path = ATTACHMENT_DIR / f"{new_id('att')}_{name}"
        try:
            path.write_text(text)
        except OSError:
            pass
        parts.append(fence(f"attachment:{kind}:{name}", text))
    return "\n\n" + "\n\n".join(parts)


async def _converse_async(conversation_id: str, user_text: str, *, attachments: list[dict[str, Any]] | None,
                          on_event: Callable[[dict[str, Any]], None] | None, paired_channel: bool) -> ChatTurn:
    reason = blocked_reason(paired_channel=paired_channel)
    if reason is not None:
        return ChatTurn(conversation_id, 0, "", reason, status="blocked")

    record = conv.get(conversation_id)
    if record is None:
        raise KeyError(conversation_id)
    agent_id = record["agent_id"]
    attachments = list(attachments or [])

    user_msg_id = conv.append_message(conversation_id, "user", user_text, attachments=attachments or None)

    settings = get_settings()
    cap = daily_budget(agent_id) * settings.chat_daily_cap_share
    if spent_today(agent_id) >= cap:
        text = (f"{agent_id} has used its chat share of today's budget "
               f"({settings.chat_daily_cap_share:.0%} of \u20ac{daily_budget(agent_id):.2f}/day); try again tomorrow.")
        msg_id = conv.append_message(conversation_id, "assistant", text, agent_id=agent_id, status="budget_exceeded")
        return ChatTurn(conversation_id, msg_id, agent_id, mask(text), status="budget_exceeded")

    record = conv.get(conversation_id)  # refresh: includes the new user message
    _maybe_summarize(agent_id, conversation_id, record)
    record = conv.get(conversation_id)  # refresh again: the summary may just have changed

    history = _build_history(record)
    task = user_text + _attachment_block(attachments)
    run_id = f"chat-{user_msg_id}"

    result = await _run_core(agent_id, task, run_id=run_id, history=history, chat=True, on_event=on_event)

    tool_steps = [step for turn in result.messages for step in turn.get("tool_steps", [])]
    msg_id = conv.append_message(conversation_id, "assistant", result.output, agent_id=agent_id,
                                 tool_steps=tool_steps or None, status=result.status, cost_eur=result.cost_eur,
                                 agent_run_id=run_id, escalations=result.escalations or None)
    stored = conv.get(conversation_id)
    reply_text = next((m["content"] for m in reversed(stored["messages"]) if m["id"] == msg_id), mask(result.output))
    return ChatTurn(conversation_id, msg_id, agent_id, reply_text, status=result.status, cost_eur=result.cost_eur,
                    tool_steps=tool_steps, escalations=result.escalations, agent_run_id=run_id)


def converse(conversation_id: str, user_text: str, *, attachments: list[dict[str, Any]] | None = None,
             on_event: Callable[[dict[str, Any]], None] | None = None, paired_channel: bool = False) -> ChatTurn:
    """Run one turn. ``on_event`` gets ``{"type": "text_delta"|"tool_start"|"tool_end"|"escalation", ...}`` dicts.

    ``current_counterparty`` is never touched here: chat is the owner talking to an agent, not an agent replying to
    a customer, so the recipient-must-be-counterparty policy rule never applies to it (I15/T2 email rules).
    """
    token = current_tainted.set(False)  # chat mode never inherits taint from an unrelated earlier run on this thread
    try:
        return run_sync(_converse_async(conversation_id, user_text, attachments=attachments, on_event=on_event,
                                        paired_channel=paired_channel))
    finally:
        current_tainted.reset(token)
