"""B: deterministic monitors - SQL checks that open and auto-resolve incidents, then alert. No LLM.

    run_monitors()  # scheduler job, every 5 minutes

Each monitor returns the problems it sees now as ``(subject, detail)`` pairs. A problem opens (or refreshes) an
incident for (monitor, subject); subjects that are no longer reported are auto-resolved. Monitors are isolated:
one that raises becomes its own P2 ``monitor_error`` incident and the others still run (its own incidents are
left untouched, because nothing is known about them). After evaluating, alerts go out for incidents that need
one (P1 repeats every 30 min until acknowledged). If a P1 stays open (unacknowledged) for over 2 h, safe mode goes
on (K7: only deliveries and customer replies continue) with one extra P1 alert; the monitors never turn it off, a
human does. New P1/P2 incidents are then handed to the Sentinel agent for a read-only diagnosis (K4, at most once per
incident, never raising: see ``diagnose_new``). The external heartbeat is pinged last, so the dead-man's switch only
hears from us when the monitor loop really ran.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from aecom import alerts, incidents
from aecom.db import AgentRun, Approval, ErrorRow, EventRow, Incident, LicenseKey, Order, session
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
SAFE_MODE_AFTER = timedelta(hours=2)  # an open (unacknowledged) P1 older than this switches safe mode on
ORDER_TOPIC_PREFIXES = ("order.", "inventory.")
UNDELIVERED_STATUSES = ("paid", "failed")
UNEMAILED_STATUSES = ("delivered", "partially_refunded")
AWAITING_CONSENT_AFTER = timedelta(hours=24)
MILESTONE_SUBJECT = "hosting_accounting_review"
DEFAULT_MIGRATION_REVENUE_EUR = 25.0  # owner decision: move off the Raspberry Pi at about USD 20-30 revenue
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


def awaiting_consent_order(now: datetime) -> list[Problem]:
    """R13b: an order held for the withdrawal-right consent is not undelivered by fault, but nobody is moving it."""
    with session() as s:
        ids = s.scalars(select(Order.id).where(Order.status == "awaiting_consent",
                                               Order.created_at < now - AWAITING_CONSENT_AFTER).order_by(Order.id)).all()
    return [(oid, f"order {oid} has waited over 24 h for the customer's withdrawal consent; "
                  "a human must obtain consent or refund it") for oid in ids]


def ledger_imbalance(now: datetime) -> list[Problem]:
    from aecom.chaos.invariants import ledger_balanced

    return _violations(ledger_balanced, "ledger")


def duplicate_key(now: datetime) -> list[Problem]:
    from aecom.chaos.invariants import no_key_on_two_orders

    if not no_key_on_two_orders():
        return []
    # Never put the key value itself in the detail: it is a sellable secret and the detail goes to ntfy and email.
    with session() as s:
        shared = s.scalars(select(LicenseKey.key).where(LicenseKey.order_id.is_not(None)).group_by(LicenseKey.key)
                           .having(func.count(func.distinct(LicenseKey.order_id)) > 1)).all()
        rows = s.execute(select(LicenseKey.id, LicenseKey.key, LicenseKey.order_id)
                         .where(LicenseKey.key.in_(shared)).order_by(LicenseKey.id)).all() if shared else []
        held = s.execute(select(Order.id, Order.quantity, func.count(LicenseKey.id))
                         .join(LicenseKey, LicenseKey.order_id == Order.id)
                         .group_by(Order.id, Order.quantity).order_by(Order.id)).all()
    by_key: dict[str, list[tuple[int, str]]] = {}
    for kid, key, oid in rows:
        by_key.setdefault(key, []).append((kid, oid))
    parts = [f"key ids {', '.join(str(k) for k, _ in v)} share one value across orders "
             f"{', '.join(sorted({o for _, o in v if o}))}" for v in by_key.values()]
    parts += [f"order {o} holds {n} keys for quantity {q or 1}" for o, q, n in held if n > (q or 1)]
    return [("license_keys", "; ".join(parts or ["licence key invariant violated"])[:2000])]


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


def _migration_threshold() -> float:
    from aecom.secrets import secrets

    try:
        return float(secrets.get("MIGRATION_REVENUE_EUR") or DEFAULT_MIGRATION_REVENUE_EUR)
    except ValueError:  # a typo in .env must not blind the monitor
        return DEFAULT_MIGRATION_REVENUE_EUR


def real_net_revenue_eur() -> float:
    """Cumulative net revenue (after refunds) from REAL store sales only: ledger lines of orders that carry a
    store-platform id (``external_id``). Demo and simulated orders, which have none, never count - otherwise the
    seeded demo data would trigger the migration reminder on day one."""
    from aecom.db import LedgerEntry

    with session() as s:
        real_orders = select(Order.id).where(Order.external_id.is_not(None))
        revenue = s.scalar(select(func.coalesce(func.sum(LedgerEntry.credit - LedgerEntry.debit), 0.0)).where(
            LedgerEntry.account == "revenue", LedgerEntry.ref.in_(real_orders))) or 0.0
        refunds = s.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0.0)).where(
            LedgerEntry.account == "refunds", LedgerEntry.ref.in_(real_orders))) or 0.0
    return round(float(revenue) - float(refunds), 2)


def revenue_milestone(now: datetime) -> list[Problem]:
    """One-time reminder to review the Pi -> cloud move and the accounting tool once cumulative net revenue
    from real store sales (after refunds) reaches MIGRATION_REVENUE_EUR (default 25).

    Never nags: once any incident for this monitor+subject is resolved, it stays silent for good. While the
    incident is open or acknowledged it keeps being reported even if refunds push revenue back below the
    threshold, so the monitor loop never auto-resolves it; only a human resolves it."""
    with session() as s:
        states = set(s.scalars(select(Incident.status).where(
            Incident.monitor == "revenue_milestone", Incident.subject == MILESTONE_SUBJECT)))
    if "resolved" in states:
        return []
    threshold = _migration_threshold()
    net = real_net_revenue_eur()
    if net < threshold and not states & set(incidents.ACTIVE):
        return []
    return [(MILESTONE_SUBJECT,
             f"Net revenue reached EUR {net:.2f} (threshold EUR {threshold:.2f}): time to review the move from the "
             "Raspberry Pi to a cloud server and to adopt an accounting tool (lexoffice or sevDesk). "
             "See docs/wiki/Raspberry-Pi.md, section Moving to the cloud. "
             "Acknowledge this incident when you have decided.")]


def refund_abuse(now: datetime) -> list[Problem]:
    """G6: refund rate over 10% (7 days, 10+ orders) or a customer with 3+ refunds in 30 days."""
    from aecom import autonomy_guard

    return autonomy_guard.refund_abuse_findings(now)


def policy_deny_spike(now: datetime) -> list[Problem]:
    """G6: one actor was denied by the policy gate 20+ times in the last hour (subject = the actor)."""
    from aecom import autonomy_guard

    return autonomy_guard.deny_spike_findings(now)


# --- stale backups (§5c) ------------------------------------------------------------------------------------

STALE_BACKUP_WARN_AFTER = timedelta(hours=26)
STALE_BACKUP_CRIT_AFTER = timedelta(days=3)
BACKUP_SUBJECT = "verified_backup"


def _backups_ever_attempted() -> bool:
    """Whether BACKUP_DIR exists at all. A directory that was never created means backups were never started on
    this machine (a fresh install, or a test/dev environment with no ``backup_dir`` of its own): nothing to alert
    on yet. Once it exists (the first `aecom backup` on a real deployment, per the Pi bootstrap guide), staleness
    is tracked from then on."""
    from aecom import backup

    return backup.backup_dir().is_dir()


def _verified_backup_age(now: datetime) -> timedelta | None:
    """Age of the newest backup whose manifest records a clean verify (set by ``backup.run_backup_job`` /
    ``run_verify_job``, or the ``aecom backup verify`` CLI). ``None`` when no backup has ever verified clean."""
    from aecom import backup

    for p in backup.list_backups():
        try:
            m = backup.read_manifest(p)
        except backup.BackupError:
            continue
        if m.get("verified"):
            return now - backup.created_at(p).replace(tzinfo=None)  # monitors compare naive UTC (``db.now()``)
    return None


def _last_off_device_failed() -> bool:
    """True only when an off-device target is configured and the newest backup's copy to it failed."""
    from aecom import backup
    from aecom.config import get_settings

    if not get_settings().backup_target:
        return False
    backups = backup.list_backups()
    if not backups:
        return False
    try:
        return backup.read_manifest(backups[0]).get("copied_off_device") is False
    except backup.BackupError:
        return False


def stale_backup(now: datetime) -> list[Problem]:
    """P2: the last verified backup is older than 26 h, or the last off-device copy failed. Catches the scheduler
    being down (nothing new is ever verified) as well as a broken off-device target."""
    if not _backups_ever_attempted():
        return []
    age = _verified_backup_age(now)
    problems = []
    if age is not None and STALE_BACKUP_WARN_AFTER <= age < STALE_BACKUP_CRIT_AFTER:
        problems.append(f"last verified backup is {age.total_seconds() / 3600:.1f} h old (max 26 h)")
    if _last_off_device_failed():
        problems.append("the last off-device copy failed")
    return [(BACKUP_SUBJECT, "; ".join(problems))] if problems else []


def stale_backup_critical(now: datetime) -> list[Problem]:
    """P1: the last verified backup is more than 3 days old, or none has ever verified clean (once backups have
    started on this machine - see ``_backups_ever_attempted``)."""
    if not _backups_ever_attempted():
        return []
    age = _verified_backup_age(now)
    if age is None:
        return [(BACKUP_SUBJECT, "no backup has ever verified clean")]
    if age >= STALE_BACKUP_CRIT_AFTER:
        return [(BACKUP_SUBJECT, f"last verified backup is {age.days} day(s) old (max 3)")]
    return []


MONITORS: dict[str, tuple[str, Monitor]] = {fn.__name__: (sev, fn) for sev, fn in (
    ("P1", undelivered_order), ("P1", unemailed_order), ("P1", ledger_imbalance), ("P1", duplicate_key),
    ("P1", order_dead_letter), ("P1", restock_failed), ("P1", stale_backup_critical),
    ("P2", other_dead_letter), ("P2", event_backlog), ("P2", stuck_run), ("P2", errors), ("P2", approval_ageing),
    ("P2", kill_switch_long), ("P2", refund_abuse), ("P2", policy_deny_spike), ("P2", stale_backup),
    ("P3", awaiting_consent_order), ("P3", revenue_milestone))}
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


def _safe_mode_check(now: datetime) -> bool:
    """Nobody reacted to a P1 for SAFE_MODE_AFTER → safe mode on + one extra P1 alert. Never switches it off."""
    from aecom.company_settings import enter_safe_mode, safe_mode_on

    if safe_mode_on():
        return True
    stale = [i for i in incidents.list_incidents(status="open", severity="P1")
             if now - i["first_seen"] > SAFE_MODE_AFTER]
    if not stale:
        return False
    inc = min(stale, key=lambda i: i["first_seen"])
    subject = f" ({inc['subject']})" if inc.get("subject") else ""
    hours = (now - inc["first_seen"]).total_seconds() / 3600
    reason = f"P1 incident {inc['id']} {inc['monitor']}{subject} open and unacknowledged for {hours:.1f} h"
    if enter_safe_mode(reason):
        alerts.route({**inc, "monitor": "safe_mode", "subject": "ON",
                      "detail": f"Safe mode is ON because {reason}. Only deliveries and customer replies continue; "
                                "no spending. Acknowledge the incident, fix it, then: "
                                "uv run aecom safe-mode off --by <you>"})
    return True


AUTO_DIAGNOSE = True  # K4: hand new P1/P2 incidents to the Sentinel; tests switch it off by monkeypatching


def diagnose_new(new_incidents: list[dict[str, Any]]) -> list[str]:
    """Ask the Sentinel to diagnose new P1/P2 incidents (after alerting). Returns the ids it ran for. Never raises.

    Skipped when AUTO_DIAGNOSE is off or the Sentinel isn't available (disabled, or scripted LLM mode without a
    sentinel script), so monitor runs stay deterministic and free. ``sentinel.diagnose`` runs at most once per incident."""
    if not AUTO_DIAGNOSE:
        return []
    ran: list[str] = []
    try:
        from aecom import sentinel

        if not sentinel.available():
            return []
        for inc in new_incidents:
            if inc["severity"] in sentinel.SEVERITIES and sentinel.diagnose(inc["id"]) is not None:
                ran.append(inc["id"])
    except Exception as exc:  # diagnosis is a bonus; the monitors must keep running
        log.exception("sentinel trigger failed")
        record_error("monitors", exc, context={"step": "sentinel"})
    return ran


def _demote_check(now: datetime) -> list[dict[str, Any]]:
    """G6: automatic demotion of promoted actions. Isolated: a failure is recorded, never raised."""
    try:
        from aecom import autonomy_guard

        return autonomy_guard.check_and_demote(now)
    except Exception as exc:
        log.exception("autonomy demotion failed")
        record_error("monitors", exc, context={"step": "autonomy_demotion"})
        return []


def run_monitors(now: datetime | None = None) -> dict[str, Any]:
    """Evaluate the monitors, alert for incidents that need it, then ping the external heartbeat."""
    t = now or db_now()
    summary = evaluate(t)
    alerted: list[str] = []
    new: list[dict[str, Any]] = []
    for inc in incidents.list_incidents(status="open"):
        if incidents.needs_alert(inc, t, repeat_after=REPEAT_ALERT_AFTER):
            alerts.route(inc)
            incidents.mark_alerted(inc["id"], now=t)
            alerted.append(inc["id"])
            if inc.get("last_alerted_at") is None:  # first alert = a new incident
                new.append(inc)
    summary["alerted"] = alerted
    summary["safe_mode"] = _safe_mode_check(t)  # containment before diagnosis
    summary["diagnosed"] = diagnose_new(new)
    summary["demoted"] = _demote_check(t)  # G6, last: it only lowers stages
    summary["open"] = incidents.open_counts()
    summary["heartbeat"] = alerts.heartbeat()
    return summary
