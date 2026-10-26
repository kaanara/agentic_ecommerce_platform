"""K2: incident lifecycle - one open problem per (monitor, subject).

Monitors call `raise_incident` while a check fails and `resolve_absent` with the subjects still failing, so incidents
close on their own when the check passes again. Humans acknowledge (stops repeat alerts) or resolve by hand.
Pure DB code: no network, no LLM. Alerting itself lives with the monitors; `needs_alert`/`mark_alerted` decide when.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from aecom.audit import audit
from aecom.db import Incident, session
from aecom.db import now as db_now

SEVERITIES = ("P1", "P2", "P3")  # P1 is the most severe
STATUSES = ("open", "acknowledged", "resolved")
ACTIVE = ("open", "acknowledged")


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity)  # lower = more severe


def _dict(i: Incident) -> dict[str, Any]:
    return {"id": i.id, "monitor": i.monitor, "severity": i.severity, "subject": i.subject, "detail": i.detail,
            "status": i.status, "first_seen": i.first_seen, "last_seen": i.last_seen,
            "last_alerted_at": i.last_alerted_at, "alert_count": i.alert_count or 0,
            "acknowledged_by": i.acknowledged_by, "acknowledged_at": i.acknowledged_at,
            "resolved_at": i.resolved_at, "notes": i.notes or ""}


def _add_note(i: Incident, by: str, note: str) -> None:
    if note:
        i.notes = f"{i.notes}\n{by}: {note}" if i.notes else f"{by}: {note}"


def raise_incident(monitor: str, severity: str, subject: str = "", detail: str = "",
                   now: datetime | None = None) -> tuple[dict[str, Any], bool]:
    """Open an incident or refresh the active one for (monitor, subject). Returns (incident, is_new).

    Severity only escalates while the incident is active. A resolved incident is never reopened."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}, got {severity!r}")
    t = now or db_now()
    escalated_from = None
    with session() as s:
        i = s.scalars(select(Incident).where(Incident.monitor == monitor, Incident.subject == subject,
                                             Incident.status.in_(ACTIVE))
                      .order_by(Incident.first_seen.desc())).first()
        is_new = i is None
        if i is None:
            i = Incident(monitor=monitor, severity=severity, subject=subject, detail=detail, status="open",
                         first_seen=t, last_seen=t, alert_count=0, notes="")
            s.add(i)
        else:
            i.last_seen = t
            if detail:
                i.detail = detail
            if _rank(severity) < _rank(i.severity):
                escalated_from, i.severity = i.severity, severity
        s.flush()
        out = _dict(i)
    if is_new:
        audit("system", "incident", "raised", {"incident_id": out["id"], "monitor": monitor, "subject": subject,
                                               "severity": severity, "detail": detail})
    elif escalated_from:
        audit("system", "incident", "escalated", {"incident_id": out["id"], "from": escalated_from, "to": severity})
    return out, is_new


def resolve_absent(monitor: str, active_subjects: Iterable[str], now: datetime | None = None) -> list[str]:
    """Auto-resolve active incidents of `monitor` whose subject is no longer failing. Returns the resolved ids."""
    t, keep = now or db_now(), set(active_subjects)
    with session() as s:
        gone = [i for i in s.scalars(select(Incident).where(Incident.monitor == monitor, Incident.status.in_(ACTIVE)))
                if i.subject not in keep]
        for i in gone:
            i.status, i.resolved_at = "resolved", t
            _add_note(i, "system", "auto-resolved: check passes again")
        ids = [i.id for i in gone]
    for iid in ids:
        audit("system", "incident", "auto_resolved", {"incident_id": iid, "monitor": monitor})
    return ids


def acknowledge(incident_id: str, by: str, note: str = "", now: datetime | None = None) -> dict[str, Any]:
    """A human has seen it: stops repeat alerts, the incident stays active until resolved."""
    with session() as s:
        i = s.get(Incident, incident_id)
        if i is None or i.status != "open":
            raise ValueError(f"incident {incident_id} is not open")
        i.status, i.acknowledged_by, i.acknowledged_at = "acknowledged", by, now or db_now()
        _add_note(i, by, note)
        out = _dict(i)
    audit(f"human:{by}", "incident", "acknowledged", {"incident_id": incident_id, "note": note})
    return out


def resolve(incident_id: str, by: str, note: str = "", now: datetime | None = None) -> dict[str, Any]:
    with session() as s:
        i = s.get(Incident, incident_id)
        if i is None or i.status not in ACTIVE:
            raise ValueError(f"incident {incident_id} is not open or acknowledged")
        i.status, i.resolved_at = "resolved", now or db_now()
        _add_note(i, by, note)
        out = _dict(i)
    audit(f"human:{by}", "incident", "resolved", {"incident_id": incident_id, "note": note})
    return out


def get_incident(incident_id: str) -> dict[str, Any] | None:
    with session() as s:
        i = s.get(Incident, incident_id)
        return _dict(i) if i else None


def list_incidents(status: str | None = None, severity: str | None = None) -> list[dict[str, Any]]:
    """Newest first (by first_seen). `status=None` returns every status."""
    with session() as s:
        q = select(Incident).order_by(Incident.first_seen.desc())
        if status:
            q = q.where(Incident.status == status)
        if severity:
            q = q.where(Incident.severity == severity)
        return [_dict(i) for i in s.scalars(q)]


def open_counts() -> dict[str, int]:
    """Active (open + acknowledged) incidents per severity."""
    counts = dict.fromkeys(SEVERITIES, 0)
    with session() as s:
        for sev in s.scalars(select(Incident.severity).where(Incident.status.in_(ACTIVE))):
            counts[sev] = counts.get(sev, 0) + 1
    return counts


def needs_alert(incident: dict[str, Any], now: datetime, repeat_after: timedelta) -> bool:
    """Alert once when an incident opens; repeat only for a P1 that is still open (unacknowledged).

    Acknowledged and resolved incidents never alert."""
    if incident["status"] != "open":
        return False
    last = incident.get("last_alerted_at")
    if last is None:
        return True
    return incident["severity"] == "P1" and now - last >= repeat_after


def mark_alerted(incident_id: str, now: datetime | None = None) -> dict[str, Any]:
    with session() as s:
        i = s.get(Incident, incident_id)
        if i is None:
            raise ValueError(f"unknown incident {incident_id}")
        i.last_alerted_at, i.alert_count = now or db_now(), (i.alert_count or 0) + 1
        out = _dict(i)
    audit("system", "incident", "alerted", {"incident_id": incident_id, "alert_count": out["alert_count"]})
    return out
