"""Read-only diagnostic tools for the Sentinel (K4), registered in the same CATALOG as the built-ins. Its gated
remediation tools (K5) are in :mod:`aecom.tools.remediation`.

Everything here only reads, except the terminal ``submit_diagnosis``, which appends a note to an incident (it never
changes the incident's status) via :func:`aecom.sentinel.append_diagnosis`. Free text is masked and long tracebacks
are shortened so a diagnosis never carries secrets and the model's context stays small.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select

from aecom.db import ErrorRow, now, session
from aecom.observability import mask
from aecom.tools.catalog import tool

TRACE_LINES = 12  # last lines of a traceback kept for the model
MAX_ERRORS = 20
MAX_TIMELINE = 100


def _short_trace(trace: str, lines: int = TRACE_LINES) -> str:
    parts = (trace or "").strip().splitlines()
    if len(parts) <= lines:
        return "\n".join(parts)
    return "\n".join(["…", *parts[-lines:]])


def _incident(i: dict[str, Any]) -> dict[str, Any]:
    return {**i, "detail": mask(i.get("detail") or ""), "notes": mask(i.get("notes") or "")}


@tool("ops")
def list_open_incidents() -> list[dict]:
    """Active incidents (open and acknowledged), newest first: id, monitor, severity, subject, detail, status, times."""
    from aecom import incidents

    active = incidents.list_incidents(status="open") + incidents.list_incidents(status="acknowledged")
    return [_incident(i) for i in sorted(active, key=lambda i: i["first_seen"], reverse=True)]


@tool("ops")
def get_incident(incident_id: str) -> dict | None:
    """One incident including its notes (earlier diagnoses, acknowledgements)."""
    from aecom import incidents

    i = incidents.get_incident(incident_id)
    return _incident(i) if i else None


@tool("ops")
def recent_errors(component: str | None = None, hours: int = 24) -> list[dict]:
    """Unexpected errors (ops_errors) of the last `hours`, newest first, optionally for one component
    (agent | gate | event_bus | api | monitors | ...). Messages are masked, tracebacks shortened to the last lines."""
    since = now() - timedelta(hours=hours)
    with session() as s:
        q = select(ErrorRow).where(ErrorRow.ts >= since).order_by(ErrorRow.id.desc()).limit(MAX_ERRORS)
        if component:
            q = q.where(ErrorRow.component == component)
        return [{"id": e.id, "ts": e.ts, "component": e.component, "run_id": e.run_id, "event_id": e.event_id,
                 "exc_type": e.exc_type, "message": mask(e.message or "")[:500],
                 "traceback": mask(_short_trace(e.traceback or "")), "context": e.context or {}}
                for e in s.scalars(q)]


@tool("ops")
def run_timeline(run_id: str) -> dict:
    """Summary and chronological timeline (runs, tool calls, approvals, audit, errors, incidents) of one run id."""
    from aecom import run_detail

    entries = run_detail.timeline(run_id)
    return {"summary": run_detail.summary(run_id), "timeline": entries[:MAX_TIMELINE],
            "truncated": len(entries) > MAX_TIMELINE}


@tool("ops")
def event_bus_status() -> dict:
    """Event counts: waiting_for_retry, queued, dead_letter."""
    from aecom.bus.events import status

    return status()


@tool("ops")
def dead_letters() -> list[dict]:
    """Dead-lettered events (id, time, topic, attempts, last error). Replay one with `replay_dead_letter` only once
    its cause is fixed."""
    from aecom.bus.events import dead_letters as _dead_letters

    return [{**d, "last_error": mask(d.get("last_error") or "")[:500]} for d in _dead_letters()[:50]]


@tool("ops")
def doctor_report() -> list[dict]:
    """The `aecom doctor` health checks (DB, schema, kill switch, event bus, invariants, approvals, errors, stuck
    runs, credentials by name, disk, backup): [{name, status: ok|warn|fail, detail}]."""
    from aecom.doctor import run_checks

    return run_checks()


# Not side-effecting for the gate: it only appends a note (never business state), so a diagnosis still lands while
# the kill switch or safe mode is on. Policy: T1 (audited), reserved for the Sentinel.
@tool("ops", terminal=True)
def submit_diagnosis(incident_id: str, summary: str, root_cause: str, evidence: list[str],
                     next_actions: list[str]) -> dict:
    """Finish: append your diagnosis to the incident's notes. root_cause = hypotheses ranked by evidence (most likely
    first); evidence = one line per observed fact; next_actions = safe steps from the runbook, each remediation tool
    you already called marked "done: <tool>(<args>) -> <result>", the rest for a human."""
    from aecom.sentinel import append_diagnosis

    result = append_diagnosis(incident_id, summary, root_cause, evidence, next_actions)
    try:
        from aecom import notify

        notify.route({"kind": "diagnosis", "text": f"Sentinel diagnosis for {incident_id}: {summary}",
                      "urgency": "normal", "agent_id": "sentinel"})
    except Exception:  # noqa: BLE001 - the diagnosis is already saved
        pass
    return result
