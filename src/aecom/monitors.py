"""B: deterministic monitors - SQL checks that open and auto-resolve incidents, then alert. No LLM.

    run_monitors()  # scheduler job, every 5 minutes

Each monitor returns the problems it sees now as ``(subject, detail)`` pairs. A problem opens (or refreshes) an
incident for (monitor, subject); subjects that are no longer reported are auto-resolved. Monitors are isolated:
one that raises becomes its own P2 ``monitor_error`` incident and the others still run (its own incidents are
left untouched, because nothing is known about them). After evaluating, alerts go out for incidents that need
one (P1 repeats every 30 min until acknowledged) and the external heartbeat is pinged last, so the dead-man's
switch only hears from us when the monitor loop really ran.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from aecom import alerts, incidents
from aecom.db import AgentRun, Approval, ErrorRow, EventRow, Order, session
from aecom.db import now as db_now
from aecom.observability import record_error

log = logging.getLogger(__name__)

ORDER_GRACE = timedelta(minutes=15)  # undelivered / unemailed orders older than this are P1
RESTOCK_WINDOW = timedelta(hours=24)
BACKLOG_MAX = 100  # waiting + queued events
BACKLOG_MAX_AGE = timedelta(minutes=10)  # oldest due event
STUCK_RUN_AFTER = timedelta(minutes=30)
ERROR_WINDOW = timedelta(hours=1)
APPROVAL_MAX_AGE = timedelta(hours=24)
KILL_SWITCH_MAX = timedelta(hours=1)
REPEAT_ALERT_AFTER = timedelta(minutes=30)
ORDER_TOPIC_PREFIXES = ("order.", "inventory.")
UNDELIVERED_STATUSES = ("paid", "failed")
UNEMAILED_STATUSES = ("delivered", "partially_refunded")
KILL_SWITCH_SINCE = "kill_switch_since"  # ops setting: ISO time the monitors first saw the kill switch on

Problem = tuple[str, str]  # (subject, detail)
Monitor = Callable[[datetime], list[Problem]]


def _violations(check: Callable[[], list[dict[str, str]]], subject: str) -> list[Problem]:
    found = check()
    return [(subject, "; ".join(v["detail"] for v in found)[:2000])] if found else []


# --- P1 ----------------------------------------------------------------------------------------------------

def undelivered_order(now: datetime) -> list[Problem]:
    with session() as s:
        rows = s.execute(select(Order.id, Order.status).where(
            Order.status.in_(UNDELIVERED_STATUSES), Order.created_at < now - ORDER_GRACE).order_by(Order.id)).all()
    return [(oid, f"order {oid} is '{st}' for over 15 min") for oid, st in rows]


def unemailed_order(now: datetime) -> list[Problem]:
    with session() as s:
        ids = s.scalars(select(Order.id).where(Order.status.in_(UNEMAILED_STATUSES), Order.email_sent.is_not(True),
                                               Order.created_at < now - ORDER_GRACE).order_by(Order.id)).all()
    return [(oid, f"order {oid} was delivered but its access email was not sent") for oid in ids]


def ledger_imbalance(now: datetime) -> list[Problem]:
    from aecom.chaos.invariants import ledger_balanced

    return _violations(ledger_balanced, "ledger")


def duplicate_key(now: datetime) -> list[Problem]:
    from aecom.chaos.invariants import no_key_on_two_orders

    return _violations(no_key_on_two_orders, "license_keys")


def _dead(order_path: bool) -> list[Problem]:
    from aecom.bus.events import dead_letters

    return [(str(d["id"]), f"event {d['id']} {d['topic']} dead after {d['attempts']} attempts: {d['last_error']}")
            for d in dead_letters() if d["topic"].startswith(ORDER_TOPIC_PREFIXES) == order_path]


def order_dead_letter(now: datetime) -> list[Problem]:
    return _dead(order_path=True)


def restock_failed(now: datetime) -> list[Problem]:
    with session() as s:
        rows = s.execute(select(EventRow.payload, EventRow.ts).where(
            EventRow.topic == "inventory.restock_failed", EventRow.ts >= now - RESTOCK_WINDOW)
            .order_by(EventRow.id)).all()
    latest: dict[str, str] = {}
    for payload, ts in rows:
        sku = str((payload or {}).get("sku") or "unknown")
        latest[sku] = f"restock of {sku} failed at {ts:%Y-%m-%d %H:%M}: {(payload or {}).get('reason', '')}"
    return sorted(latest.items())


# --- P2 ----------------------------------------------------------------------------------------------------

def other_dead_letter(now: datetime) -> list[Problem]:
    return _dead(order_path=False)


def event_backlog(now: datetime) -> list[Problem]:
    from aecom.bus.events import status

    st = status()
    pending = st["waiting_for_retry"] + st["queued"]
    with session() as s:
        oldest = s.scalar(select(func.min(EventRow.ts)).where(
            EventRow.processed.is_(False), EventRow.dead.is_not(True),
            or_(EventRow.next_attempt_at.is_(None), EventRow.next_attempt_at <= now)))
    problems = []
    if pending > BACKLOG_MAX:
        problems.append(f"{pending} unprocessed events (max {BACKLOG_MAX})")
    if oldest is not None and oldest < now - BACKLOG_MAX_AGE:
        problems.append(f"oldest due event is from {oldest:%Y-%m-%d %H:%M}")
    return [("event_bus", "; ".join(problems))] if problems else []


def stuck_run(now: datetime) -> list[Problem]:
    with session() as s:
        rows = s.execute(select(AgentRun.id, AgentRun.agent_id).where(
            AgentRun.status == "running", AgentRun.started_at < now - STUCK_RUN_AFTER).order_by(AgentRun.id)).all()
    return [(rid, f"run {rid} of {agent} has been 'running' for over 30 min") for rid, agent in rows]


def errors(now: datetime) -> list[Problem]:
    with session() as s:
        n = s.scalar(select(func.count(ErrorRow.id)).where(ErrorRow.ts >= now - ERROR_WINDOW)) or 0
    return [("ops_errors", f"{n} unexpected error(s) in the last hour (aecom errors)")] if n else []


def approval_ageing(now: datetime) -> list[Problem]:
    with session() as s:
        rows = s.execute(select(Approval.id, Approval.agent_id, Approval.tool_name).where(
            Approval.status == "pending", Approval.created_at < now - APPROVAL_MAX_AGE).order_by(Approval.id)).all()
    return [(aid, f"approval {aid} ({agent}: {tool}) pending for over 24 h") for aid, agent, tool in rows]


def kill_switch_long(now: datetime) -> list[Problem]:
    from aecom.company_settings import get_setting, kill_switch_on, set_setting

    since_raw = get_setting(KILL_SWITCH_SINCE) or ""
    if not kill_switch_on():
        if since_raw:
            set_setting(KILL_SWITCH_SINCE, "")
        return []
    if not since_raw:
        set_setting(KILL_SWITCH_SINCE, now.isoformat())
        return []
    since = datetime.fromisoformat(since_raw)
    if now - since > KILL_SWITCH_MAX:
        return [("kill_switch", f"kill switch on since {since:%Y-%m-%d %H:%M} (deliveries are stopped)")]
    return []


MONITORS: dict[str, tuple[str, Monitor]] = {fn.__name__: (sev, fn) for sev, fn in (
    ("P1", undelivered_order), ("P1", unemailed_order), ("P1", ledger_imbalance), ("P1", duplicate_key),
    ("P1", order_dead_letter), ("P1", restock_failed),
    ("P2", other_dead_letter), ("P2", event_backlog), ("P2", stuck_run), ("P2", errors), ("P2", approval_ageing),
    ("P2", kill_switch_long))}
MONITOR_ERROR = "monitor_error"


def evaluate(now: datetime | None = None) -> dict[str, Any]:
    """Run every monitor once: raise incidents for current problems, auto-resolve fixed ones."""
    t = now or db_now()
    failing: dict[str, int] = {}
    broken: list[str] = []
    resolved: list[str] = []
    for name, (severity, fn) in MONITORS.items():
        try:
            problems = fn(t)
            for subject, detail in problems:
                incidents.raise_incident(name, severity, subject, detail, now=t)
            resolved += incidents.resolve_absent(name, [subj for subj, _ in problems], now=t)
            if problems:
                failing[name] = len(problems)
        except Exception as exc:  # one broken monitor must not blind the others
            log.exception("monitor %s failed", name)
            record_error("monitors", exc, context={"monitor": name})
            broken.append(name)
            try:
                incidents.raise_incident(MONITOR_ERROR, "P2", name, f"{type(exc).__name__}: {exc}"[:2000], now=t)
            except Exception:
                log.exception("could not raise monitor_error for %s", name)
    resolved += incidents.resolve_absent(MONITOR_ERROR, broken, now=t)
    return {"failing": failing, "monitor_errors": broken, "resolved": resolved}


def run_monitors(now: datetime | None = None) -> dict[str, Any]:
    """Evaluate the monitors, alert for incidents that need it, then ping the external heartbeat."""
    t = now or db_now()
    summary = evaluate(t)
    alerted: list[str] = []
    for inc in incidents.list_incidents(status="open"):
        if incidents.needs_alert(inc, t, repeat_after=REPEAT_ALERT_AFTER):
            alerts.route(inc)
            incidents.mark_alerted(inc["id"], now=t)
            alerted.append(inc["id"])
    summary["alerted"] = alerted
    summary["open"] = incidents.open_counts()
    summary["heartbeat"] = alerts.heartbeat()
    return summary
