"""Data access for chat conversations with agents (ops_conversations / ops_messages).

Everything stored here is masked first (``observability.mask``), so a secret an agent or the owner pastes never
lands in the database. The conversation list and the chat page read only through these functions.
SQLite does not enforce the ON DELETE CASCADE here (no foreign_keys pragma), so ``delete`` removes messages itself.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from aecom.audit import audit
from aecom.db import ChatMessage, Conversation, now, session
from aecom.observability import mask

TITLE_MAX = 60


def auto_title(text: str) -> str:
    """Deterministic title from the first user message (no LLM call): first line, collapsed spaces, 60 chars."""
    line = " ".join((text or "").strip().splitlines()[0].split()) if (text or "").strip() else ""
    line = mask(line)
    if len(line) <= TITLE_MAX:
        return line or "New chat"
    return line[: TITLE_MAX - 1].rstrip() + "…"


def _conv_dict(c: Conversation) -> dict[str, Any]:
    return {
        "id": c.id, "title": c.title, "agent_id": c.agent_id, "channel": c.channel, "started_by": c.started_by,
        "owner": c.owner, "summary": c.summary, "archived": c.archived, "unread": c.unread,
        "total_cost_eur": c.total_cost_eur, "created_at": c.created_at, "updated_at": c.updated_at,
    }


def _msg_dict(m: ChatMessage) -> dict[str, Any]:
    return {
        "id": m.id, "conversation_id": m.conversation_id, "role": m.role, "agent_id": m.agent_id, "content": m.content,
        "tool_steps": m.tool_steps or [], "status": m.status, "cost_eur": m.cost_eur, "agent_run_id": m.agent_run_id,
        "escalations": m.escalations or [], "attachments": m.attachments or [], "created_at": m.created_at,
    }


def create(agent_id: str, owner: str = "", *, title: str = "", channel: str = "dashboard",
           started_by: str = "owner") -> str:
    with session() as s:
        conv = Conversation(agent_id=agent_id, owner=owner, title=mask(title), channel=channel, started_by=started_by)
        s.add(conv)
        s.flush()
        return conv.id


def get(conversation_id: str) -> dict[str, Any] | None:
    """The conversation with its messages (oldest first), or None."""
    with session() as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None:
            return None
        msgs = s.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
                         .order_by(ChatMessage.id)).all()
        return {**_conv_dict(conv), "messages": [_msg_dict(m) for m in msgs]}


def list_conversations(*, include_archived: bool = False, only_unread: bool = False, search: str = "",
                       limit: int = 50) -> list[dict[str, Any]]:
    with session() as s:
        q = select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
        if not include_archived:
            q = q.where(Conversation.archived.is_(False))
        if only_unread:
            q = q.where(Conversation.unread > 0)
        if search.strip():
            q = q.where(Conversation.title.ilike(f"%{search.strip()}%"))
        return [_conv_dict(c) for c in s.scalars(q)]


def append_message(conversation_id: str, role: str, content: str, *, agent_id: str | None = None,
                   tool_steps: list[Any] | None = None, status: str = "ok", cost_eur: float = 0.0,
                   agent_run_id: str | None = None, escalations: list[Any] | None = None,
                   attachments: list[Any] | None = None) -> int:
    """Store one message (content is masked). Updates cost, recency and the unread counter of the conversation."""
    if role not in ("user", "assistant"):
        raise ValueError("role must be 'user' or 'assistant'")
    with session() as s:
        conv = s.get(Conversation, conversation_id)
        if conv is None:
            raise KeyError(conversation_id)
        msg = ChatMessage(conversation_id=conversation_id, role=role, content=mask(content or ""), agent_id=agent_id,
                          tool_steps=tool_steps, status=status, cost_eur=cost_eur, agent_run_id=agent_run_id,
                          escalations=escalations, attachments=attachments)
        s.add(msg)
        conv.total_cost_eur = (conv.total_cost_eur or 0.0) + cost_eur
        conv.updated_at = now()
        if not conv.title and role == "user":
            conv.title = auto_title(content)
        if role == "assistant" and conv.started_by == "agent":
            conv.unread = (conv.unread or 0) + 1
        s.flush()
        return msg.id


def set_summary(conversation_id: str, summary: str) -> None:
    with session() as s:
        if conv := s.get(Conversation, conversation_id):
            conv.summary = mask(summary)


def rename(conversation_id: str, title: str) -> None:
    with session() as s:
        if conv := s.get(Conversation, conversation_id):
            conv.title = mask(title.strip())[:200]


def archive(conversation_id: str, flag: bool = True) -> None:
    with session() as s:
        if conv := s.get(Conversation, conversation_id):
            conv.archived = flag


def mark_read(conversation_id: str) -> None:
    with session() as s:
        if conv := s.get(Conversation, conversation_id):
            conv.unread = 0


def unread_total() -> int:
    with session() as s:
        return int(s.scalar(select(func.coalesce(func.sum(Conversation.unread), 0))) or 0)


def delete(conversation_id: str, by: str = "") -> bool:
    """Hard delete (messages first: SQLite does not cascade). Attachment files are removed by the chat layer."""
    with session() as s:
        if s.get(Conversation, conversation_id) is None:
            return False
        s.execute(sa_delete(ChatMessage).where(ChatMessage.conversation_id == conversation_id))
        s.execute(sa_delete(Conversation).where(Conversation.id == conversation_id))
    audit(f"human:{by}" if by else "system", "conversation_delete", "ok", {"conversation_id": conversation_id})
    return True


def purge_older_than(days: int) -> int:
    """Retention: delete conversations whose last activity is older than ``days`` (0 or less = keep forever)."""
    if days <= 0:
        return 0
    cutoff = now() - timedelta(days=days)
    with session() as s:
        ids = list(s.scalars(select(Conversation.id).where(Conversation.updated_at < cutoff)))
        if ids:
            s.execute(sa_delete(ChatMessage).where(ChatMessage.conversation_id.in_(ids)))
            s.execute(sa_delete(Conversation).where(Conversation.id.in_(ids)))
    if ids:
        audit("system", "conversation_purge", "ok", {"count": len(ids), "older_than_days": days})
    return len(ids)
