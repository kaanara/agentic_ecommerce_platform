"""Human-in-the-loop decisions. Used by the dashboard, the API and the CLI."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import select

from aecom.audit import audit
from aecom.bus.events import publish
from aecom.company_settings import kill_switch_on
from aecom.context import current_agent
from aecom.db import Approval, now, session
from aecom.tools.catalog import CATALOG


def _expired(a: Approval) -> bool:
    return a.status == "pending" and a.expires_at is not None and a.expires_at <= now()


def expire_stale() -> int:
    """Mark pending approvals past their ``expires_at`` as 'expired'. Returns how many were marked."""
    with session() as s:
        stale = s.scalars(select(Approval).where(Approval.status == "pending", Approval.expires_at.is_not(None),
                                                 Approval.expires_at <= now())).all()
        for a in stale:
            a.status = "expired"
        return len(stale)


def list_approvals(status: str | None = "pending") -> list[dict[str, Any]]:
    expire_stale()
    with session() as s:
        q = select(Approval).order_by(Approval.created_at.desc())
        if status:
            q = q.where(Approval.status == status)
        return [{"id": a.id, "agent_id": a.agent_id, "tool_name": a.tool_name, "args": a.args, "risk_tier": a.risk_tier,
                 "reason": a.reason, "context": a.context, "status": a.status, "run_id": a.run_id,
                 "created_at": a.created_at, "expires_at": a.expires_at, "decided_by": a.decided_by, "result": a.result} for a in s.scalars(q)]


def _execute(tool_name: str, agent_id: str, args: dict[str, Any]) -> Any:
    token = current_agent.set(agent_id)
    try:
        if tool_name in CATALOG:
            return CATALOG[tool_name].fn(**args)
        if tool_name.startswith("mcp:"):
            from aecom.registry.mcp_manager import call_mcp_tool

            server, _, tool = tool_name.removeprefix("mcp:").partition("/")
            return asyncio.run(call_mcp_tool(server, tool, args))
        raise ValueError(f"unknown tool {tool_name}")
    finally:
        current_agent.reset(token)


def approve(approval_id: str, by: str, note: str = "", args_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Approve and execute the parked call. `args_override` lets the human edit before execution.

    Refused while the kill switch is on; the approval stays pending so it can be approved once it is off."""
    if kill_switch_on():
        raise ValueError("kill switch is on - the action was not executed; turn it off first")
    with session() as s:
        a = s.get(Approval, approval_id)
        if a is None or a.status != "pending":
            raise ValueError(f"approval {approval_id} is not pending")
        expired = _expired(a)
        if expired:
            a.status = "expired"
        else:
            a.status, a.decided_by, a.decision_note, a.decided_at = "approved", by, note, now()
        if args_override:
            a.args = {**a.args, **args_override}
        tool_name, agent_id, args, run_id = a.tool_name, a.agent_id, dict(a.args), a.run_id
    if expired:
        audit(f"human:{by}", tool_name, "expired", {"approval_id": approval_id}, run_id)
        raise ValueError("approval expired - ask the agent to re-request")
    audit(f"human:{by}", tool_name, "approved", {"approval_id": approval_id, "args": args, "note": note}, run_id)
    try:
        result = _execute(tool_name, agent_id, args)
        status, text = "executed", json.dumps(result, default=str)
    except Exception as exc:
        status, text = "failed", f"{type(exc).__name__}: {exc}"
    with session() as s:
        a = s.get(Approval, approval_id)
        a.status, a.result = status, text[:4000]
    audit(agent_id, tool_name, status, {"approval_id": approval_id, "result": text[:500]}, run_id)
    publish("approval.resolved", {"approval_id": approval_id, "tool": tool_name, "agent": agent_id, "status": status,
                                  "note": note}, source="human")
    return {"approval_id": approval_id, "status": status, "result": text}


def reject(approval_id: str, by: str, note: str = "") -> dict[str, Any]:
    with session() as s:
        a = s.get(Approval, approval_id)
        if a is None or a.status != "pending":
            raise ValueError(f"approval {approval_id} is not pending")
        expired = _expired(a)
        if expired:
            a.status = "expired"
        else:
            a.status, a.decided_by, a.decision_note, a.decided_at = "rejected", by, note, now()
        tool_name, agent_id, run_id = a.tool_name, a.agent_id, a.run_id
    if expired:
        raise ValueError(f"approval {approval_id} is not pending (expired)")
    audit(f"human:{by}", tool_name, "rejected", {"approval_id": approval_id, "note": note}, run_id)
    publish("approval.resolved", {"approval_id": approval_id, "tool": tool_name, "agent": agent_id, "status": "rejected",
                                  "note": note}, source="human")
    return {"approval_id": approval_id, "status": "rejected"}
