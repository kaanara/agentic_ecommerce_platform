"""G6: automatic demotion and abuse detection for the Phase 5 autonomy stages (see ``aecom.autonomy``).

Pure trigger functions look at the database and return ``Trigger(tools, key, reason)`` for every tripped condition;
``check_and_demote`` sends each affected promoted tool back to stage 1 (by "system") and raises a P2 incident per
tool. It never promotes, leaves stage-1 tools alone, is idempotent and never raises. The detectors for refund abuse
and policy-deny spikes are shared with the monitors of the same name, so humans hear about them even when nothing
is promoted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from aecom import autonomy, incidents
from aecom.db import AuditLog, Order, Refund, session
from aecom.db import now as db_now
from aecom.observability import record_error

log = logging.getLogger(__name__)

KILL_SWITCH_LOOKBACK = timedelta(hours=24)
REFUND_WINDOW = timedelta(days=7)
REFUND_RATE_MAX = 0.10  # refund rate above this ...
REFUND_RATE_MIN_ORDERS = 10  # ... with at least this many orders
CUSTOMER_REFUND_WINDOW = timedelta(days=30)
CUSTOMER_REFUNDS_MAX = 2  # more than this many refunds for one customer
DENY_WINDOW = timedelta(hours=1)
DENY_SPIKE_MIN = 20  # denies by one actor
ROAS_MIN = 1.0
ROAS_MIN_SPEND_EUR = 100.0
GROSS_MARGIN_MIN_PCT = 50.0
GROSS_MARGIN_MIN_ORDERS = 10
MONITOR = "autonomy_demotion"


@dataclass(frozen=True)
class Trigger:
    tools: tuple[str, ...]
    key: str
    reason: str


def _all_promoted() -> tuple[str, ...]:
    return tuple(autonomy.promoted_tools())


# --- detectors shared with the monitors: [(subject, detail)] ------------------------------------------------
def refund_abuse_findings(now: datetime) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    start = now - REFUND_WINDOW
    with session() as s:
        orders = s.scalar(select(func.count(Order.id)).where(Order.created_at >= start, Order.created_at <= now)) or 0
        refunded = s.scalar(select(func.count(func.distinct(Refund.order_id))).join(Order, Order.id == Refund.order_id)
                            .where(Order.created_at >= start, Order.created_at <= now)) or 0
        per_customer = s.execute(
            select(Order.customer_id, func.count(Refund.id)).join(Order, Order.id == Refund.order_id)
            .where(Refund.created_at >= now - CUSTOMER_REFUND_WINDOW, Refund.created_at <= now)
            .group_by(Order.customer_id).having(func.count(Refund.id) > CUSTOMER_REFUNDS_MAX)
            .order_by(Order.customer_id)).all()
    if orders >= REFUND_RATE_MIN_ORDERS and refunded / orders > REFUND_RATE_MAX:
        findings.append(("refunds", f"{refunded} of {orders} orders of the last 7 days were refunded "
                                    f"({100 * refunded / orders:.0f}%, limit {100 * REFUND_RATE_MAX:.0f}%)"))
    findings += [(cid, f"customer {cid} has {n} refunds in the last 30 days (limit {CUSTOMER_REFUNDS_MAX})")
                 for cid, n in per_customer]
    return findings


def deny_spike_findings(now: datetime) -> list[tuple[str, str]]:
    with session() as s:
        rows = s.execute(select(AuditLog.actor, func.count(AuditLog.id)).where(
            AuditLog.decision == "deny", AuditLog.ts >= now - DENY_WINDOW, AuditLog.ts <= now)
            .group_by(AuditLog.actor).having(func.count(AuditLog.id) >= DENY_SPIKE_MIN)
            .order_by(AuditLog.actor)).all()
    return [(actor, f"{actor} was denied by the policy gate {n} times in the last hour (limit {DENY_SPIKE_MIN - 1})")
            for actor, n in rows]


# --- triggers ----------------------------------------------------------------------------------------------
def containment(now: datetime) -> list[Trigger]:
    from aecom.company_settings import kill_switch_on, safe_mode_on

    reason = None
    if safe_mode_on():
        reason = "safe mode is on"
    elif kill_switch_on():
        reason = "the kill switch is on"
    else:
        with session() as s:
            hit = s.execute(select(AuditLog.action).where(
                ((AuditLog.action == "kill_switch") | (AuditLog.action == "safe_mode")) & (AuditLog.decision == "on"),
                AuditLog.ts >= now - KILL_SWITCH_LOOKBACK, AuditLog.ts <= now).order_by(AuditLog.id.desc())).first()
        if hit:
            reason = ("the kill switch was switched on" if hit[0] == "kill_switch" else "safe mode was entered") \
                + " in the last 24 h"
    return [Trigger(_all_promoted(), "containment", f"containment: {reason}")] if reason else []


def p1_incident(now: datetime) -> list[Trigger]:
    active = [i for i in incidents.list_incidents(severity="P1") if i["status"] in incidents.ACTIVE]
    if not active:
        return []
    i = active[0]
    subject = f" ({i['subject']})" if i.get("subject") else ""
    return [Trigger(_all_promoted(), "p1_incident", f"P1 incident {i['id']} {i['monitor']}{subject} is {i['status']}")]


def refund_abuse(now: datetime) -> list[Trigger]:
    found = refund_abuse_findings(now)
    return [Trigger(("refund_order",), "refund_abuse", f"refund abuse: {'; '.join(d for _, d in found)}")] if found else []


def policy_deny_spike(now: datetime) -> list[Trigger]:
    found = deny_spike_findings(now)
    return [Trigger(_all_promoted(), "policy_deny_spike", f"policy-deny spike: {'; '.join(d for _, d in found)}")] \
        if found else []


def kpi_drop(now: datetime) -> list[Trigger]:
    from aecom import metrics

    start, end = metrics.period("last_7_days", now)
    pnl, kpis = metrics.pnl(start, end), metrics.kpis(start, end)
    out: list[Trigger] = []
    roas, spend = kpis["roas"], pnl["marketing_expense"]
    if roas is not None and spend >= ROAS_MIN_SPEND_EUR and roas < ROAS_MIN:
        out.append(Trigger(("launch_campaign",), "roas_low",
                           f"ROAS {roas:.2f} is below {ROAS_MIN:.1f} on EUR {spend:.2f} marketing spend in 7 days"))
    if kpis["orders"] >= GROSS_MARGIN_MIN_ORDERS and kpis["gross_margin_pct"] < GROSS_MARGIN_MIN_PCT:
        out.append(Trigger(("update_price",), "gross_margin_low",
                           f"gross margin {kpis['gross_margin_pct']:.1f}% is below {GROSS_MARGIN_MIN_PCT:.0f}% "
                           f"over {kpis['orders']} orders in 7 days"))
    return out


TRIGGERS = (containment, p1_incident, refund_abuse, policy_deny_spike, kpi_drop)


def evaluate_triggers(now: datetime | None = None) -> list[Trigger]:
    """Every tripped trigger (all are evaluated; one that fails is recorded and skipped)."""
    t = now or db_now()
    out: list[Trigger] = []
    for fn in TRIGGERS:
        try:
            out += fn(t)
        except Exception as exc:
            log.exception("autonomy trigger %s failed", fn.__name__)
            record_error("autonomy_guard", exc, context={"trigger": fn.__name__})
    return out


def check_and_demote(now: datetime | None = None) -> list[dict[str, Any]]:
    """Demote every promoted tool a trigger names, raise a P2 incident per demoted tool. Never raises."""
    t = now or db_now()
    done: list[dict[str, Any]] = []
    try:
        for trig in evaluate_triggers(t):
            for tool in trig.tools:
                try:
                    if autonomy.current_stage(tool) <= 1:
                        continue
                    autonomy.demote(tool, trig.reason, by="system")
                    incidents.raise_incident(MONITOR, "P2", tool, f"{tool} demoted to stage 1: {trig.reason}", now=t)
                    done.append({"tool": tool, "reason": trig.reason})
                except Exception as exc:
                    log.exception("could not demote %s", tool)
                    record_error("autonomy_guard", exc, context={"tool": tool, "trigger": trig.key})
    except Exception as exc:
        log.exception("autonomy check failed")
        record_error("autonomy_guard", exc, context={"step": "check_and_demote"})
    return done
