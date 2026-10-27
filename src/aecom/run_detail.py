"""D1/D2: what agents are doing right now, and what happened in any run.

Pure read-only helpers over the ops tables (no Streamlit), shared by the dashboard (``Agents`` and ``Run detail``
pages) and the Sentinel agent. Every function returns plain dicts/lists, never ORM objects, so callers can use
the results after the session is closed.

- :func:`running_now` - agent runs still marked ``running``, with their age and a ``stuck`` flag.
- :func:`timeline` - one chronologically ordered list of everything recorded for a run id.
- :func:`summary` - counts and totals for a run id.

A *run id* is the graph-level id (``AgentRun.run_id``, also stored on tool calls, approvals, audit and error rows).
An ``AgentRun.id`` (one agent's row) is accepted too and resolved to its run id.

Free text is passed through :func:`aecom.observability.mask` again here, on top of the masking done at the source.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from aecom.db import AgentRun, Approval, AuditLog, Decision, ErrorRow, Incident, ToolCallLog, now, session
from aecom.observability import mask

STUCK_AFTER = timedelta(minutes=30)
_TEXT_MAX = 300

# tie-break for entries with the same timestamp: a run starts before its calls and ends after them
_KIND_ORDER = {"run_start": 0, "tool_call": 1, "approval_created": 2, "audit": 3, "decision": 4, "error": 5,
               "incident": 6, "approval_decided": 7, "run_end": 8}


def _text(value: Any, limit: int = _TEXT_MAX) -> str:
    if value is None:
        return ""
    s = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    s = mask(s)
    return s if len(s) <= limit else s[:limit] + " …"


def _resolve(s: Any, run_id: str) -> str:
    """Accept an AgentRun row id as well as a graph-level run id."""
    row = s.get(AgentRun, run_id)
    return row.run_id if row is not None else run_id


def running_now(stuck_after: timedelta = STUCK_AFTER, *, at: datetime | None = None) -> list[dict[str, Any]]:
    """Agent runs whose status is still ``running``, oldest first.

    Each item: ``{id, run_id, agent_id, model, task, started_at, age_s, stuck, steps, tool_calls, cost_eur}``.
    ``stuck`` is True when the run has been running for at least ``stuck_after``. ``at`` overrides "now" (tests).
    """
    ref = at or now()
    with session() as s:
        rows = s.scalars(select(AgentRun).where(AgentRun.status == "running").order_by(AgentRun.started_at)).all()
        out = []
        for r in rows:
            age = ref - r.started_at
            out.append({"id": r.id, "run_id": r.run_id, "agent_id": r.agent_id, "model": r.model,
                        "task": _text(r.task, 160), "started_at": r.started_at, "age_s": max(age.total_seconds(), 0.0),
                        "stuck": age >= stuck_after, "steps": r.steps, "tool_calls": r.tool_calls,
                        "cost_eur": round(r.cost_eur or 0.0, 6)})
    return out


def _entry(ts: datetime | None, kind: str, actor: str, summary: str, ref: str) -> dict[str, Any]:
    return {"ts": ts, "kind": kind, "actor": actor or "", "summary": summary, "ref": ref}


def timeline(run_id: str) -> list[dict[str, Any]]:
    """Everything recorded for ``run_id``, ordered by time. Each entry: ``{ts, kind, actor, summary, ref}``.

    ``kind`` is one of: run_start, run_end, tool_call, approval_created, approval_decided, audit, error,
    decision, incident. ``ref`` is the id of the source row (e.g. ``apr_...``, ``tool_call:12``, ``error:3``).
    Decisions have no run id, so they are matched best effort: created while one of the run's agents was
    running, and decided by that agent. Incidents are matched when the run id appears in subject or detail.
    """
    entries: list[dict[str, Any]] = []
    with session() as s:
        run_id = _resolve(s, run_id)
        runs = s.scalars(select(AgentRun).where(AgentRun.run_id == run_id).order_by(AgentRun.started_at)).all()
        for r in runs:
            entries.append(_entry(r.started_at, "run_start", r.agent_id,
                                  _text(f"started ({r.model}): {r.task}"), r.id))
            if r.ended_at is not None or r.status != "running":
                entries.append(_entry(r.ended_at or r.started_at, "run_end", r.agent_id,
                                      _text(f"{r.status} · {r.steps} steps · {r.tool_calls} tool calls · "
                                            f"€{(r.cost_eur or 0.0):.4f} · {r.output}"), r.id))
        for t in s.scalars(select(ToolCallLog).where(ToolCallLog.run_id == run_id).order_by(ToolCallLog.id)):
            parts = [f"{t.tool_name} → {t.decision}{'' if t.ok else ' (failed)'}"]
            if t.args_summary:
                parts.append(f"args: {t.args_summary}")
            if t.result_summary:
                parts.append(f"result: {t.result_summary}")
            entries.append(_entry(t.ts, "tool_call", t.agent_id, _text(" · ".join(parts)), f"tool_call:{t.id}"))
        for a in s.scalars(select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at)):
            entries.append(_entry(a.created_at, "approval_created", a.agent_id,
                                  _text(f"{a.tool_name} escalated ({a.risk_tier}): {a.reason}"), a.id))
            if a.decided_at is not None:
                entries.append(_entry(a.decided_at, "approval_decided", a.decided_by or "",
                                      _text(f"{a.tool_name} {a.status}"
                                            + (f" · {a.decision_note}" if a.decision_note else "")), a.id))
        for au in s.scalars(select(AuditLog).where(AuditLog.run_id == run_id).order_by(AuditLog.id)):
            entries.append(_entry(au.ts, "audit", au.actor, _text(f"{au.action} {au.decision}: {_text(au.detail)}"),
                                  f"audit:{au.id}"))
        for e in s.scalars(select(ErrorRow).where(ErrorRow.run_id == run_id).order_by(ErrorRow.id)):
            entries.append(_entry(e.ts, "error", e.component, _text(f"{e.exc_type}: {e.message}"), f"error:{e.id}"))
        for d in _decisions(s, runs):
            entries.append(_entry(d.created_at, "decision", d.decided_by,
                                  _text(f"[{d.category}] {d.title} → {d.choice}"), d.id))
        if run_id:
            like = f"%{run_id}%"
            for i in s.scalars(select(Incident).where(or_(Incident.subject.like(like), Incident.detail.like(like)))
                               .order_by(Incident.first_seen)):
                entries.append(_entry(i.first_seen, "incident", i.monitor,
                                      _text(f"{i.severity} {i.status} · {i.subject}: {i.detail}"), i.id))
    entries.sort(key=lambda x: (x["ts"] or datetime.min, _KIND_ORDER.get(x["kind"], 9)))
    return entries


def _decisions(s: Any, runs: list[AgentRun]) -> list[Decision]:
    found: dict[str, Decision] = {}
    for r in runs:
        q = select(Decision).where(Decision.decided_by == r.agent_id, Decision.created_at >= r.started_at)
        if r.ended_at is not None:
            q = q.where(Decision.created_at <= r.ended_at)
        for d in s.scalars(q):
            found[d.id] = d
    return sorted(found.values(), key=lambda d: d.created_at)


def summary(run_id: str) -> dict[str, Any]:
    """``{run_id, agents, tools_used, tool_calls, approvals, errors, cost_eur, status, started_at, ended_at}``.

    ``agents`` and ``tools_used`` are sorted unique names; ``approvals`` and ``errors`` are counts.
    ``status`` is ``unknown`` (no agent run), ``running`` (any still running), ``error`` (any errored),
    ``vetoed`` (any vetoed), else ``ok``.
    """
    with session() as s:
        run_id = _resolve(s, run_id)
        runs = s.scalars(select(AgentRun).where(AgentRun.run_id == run_id)).all()
        calls = s.scalars(select(ToolCallLog).where(ToolCallLog.run_id == run_id)).all()
        n_approvals = len(s.scalars(select(Approval.id).where(Approval.run_id == run_id)).all())
        n_errors = len(s.scalars(select(ErrorRow.id).where(ErrorRow.run_id == run_id)).all())
        statuses = {r.status for r in runs}
        status = ("unknown" if not runs else "running" if "running" in statuses else "error" if "error" in statuses
                  else "vetoed" if "vetoed" in statuses else "ok")
        ended = [r.ended_at for r in runs if r.ended_at is not None]
        return {"run_id": run_id, "agents": sorted({r.agent_id for r in runs}),
                "tools_used": sorted({c.tool_name for c in calls}),
                "tool_calls": len([c for c in calls if c.decision != "error"]),
                "approvals": n_approvals, "errors": n_errors,
                "cost_eur": round(sum(r.cost_eur or 0.0 for r in runs), 6), "status": status,
                "started_at": min((r.started_at for r in runs), default=None),
                "ended_at": max(ended) if ended and status != "running" else None}


def recent_run_ids(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent run ids (newest first) with their agents, for pickers: ``{run_id, started_at, agents, status}``."""
    with session() as s:
        rows = s.scalars(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit * 5)).all()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = out.setdefault(r.run_id, {"run_id": r.run_id, "started_at": r.started_at, "agents": [],
                                             "status": r.status})
            item["started_at"] = min(item["started_at"], r.started_at)
            if r.agent_id not in item["agents"]:
                item["agents"].append(r.agent_id)
            if r.status != "ok":
                item["status"] = r.status
            if len(out) > limit:
                break
    return list(out.values())[:limit]
