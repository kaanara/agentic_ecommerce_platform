"""I15: human review of shared memory.

`remember(shared=True)` never reaches other agents directly: it lands as `pending_review`, with provenance (who wrote
it, in which run, and whether that run had read untrusted content). A human approves it (status `active`, recalled by
every agent) or rejects it (status `rejected`, never recalled). Pure DB code: no network, no LLM.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aecom.audit import audit
from aecom.db import Memory, session


def _dict(m: Memory) -> dict[str, Any]:
    return {"id": m.id, "namespace": m.namespace, "content": m.content, "source_agent": m.source_agent,
            "source_run_id": m.source_run_id, "tainted": bool(m.tainted), "status": m.status,
            "reviewed_by": m.reviewed_by, "created_at": m.created_at}


def list_pending() -> list[dict[str, Any]]:
    """Shared memories waiting for a human, oldest first."""
    with session() as s:
        return [_dict(m) for m in s.scalars(select(Memory).where(Memory.status == "pending_review")
                                            .order_by(Memory.created_at))]


def _review(memory_id: str, by: str, status: str, note: str = "") -> dict[str, Any]:
    if not (by or "").strip():
        raise ValueError("a reviewer name is required (--by NAME)")
    with session() as s:
        m = s.get(Memory, memory_id)
        if m is None:
            raise ValueError(f"unknown memory {memory_id}")
        if m.status != "pending_review":
            raise ValueError(f"memory {memory_id} is {m.status}, not pending_review")
        m.status, m.reviewed_by = status, by
        s.flush()
        out = _dict(m)
    audit(f"human:{by}", "memory_review", "approved" if status == "active" else "rejected",
          {"memory_id": memory_id, "source_agent": out["source_agent"], "tainted": out["tainted"], "note": note},
          run_id=out["source_run_id"])
    return out


def approve(memory_id: str, by: str, note: str = "") -> dict[str, Any]:
    """Make a pending shared memory visible to every agent."""
    return _review(memory_id, by, "active", note)


def reject(memory_id: str, by: str, note: str = "") -> dict[str, Any]:
    """Discard a pending shared memory; it is kept for the record but never recalled."""
    return _review(memory_id, by, "rejected", note)
