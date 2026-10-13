from __future__ import annotations

from typing import Any

from aecom.db import AuditLog, session


def audit(actor: str, action: str, decision: str, detail: dict[str, Any] | None = None, run_id: str | None = None) -> None:
    with session() as s:
        s.add(AuditLog(actor=actor, action=action, decision=decision, detail=detail or {}, run_id=run_id))
