"""Business invariants that must hold after any run, however many faults were injected.

Each check returns a list of violations ``{"invariant": name, "detail": text}``; an empty list means it holds.
Money is compared in integer cents so float noise never counts as a violation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func, select

from aecom.db import AgentRun, Campaign, EventRow, LedgerEntry, LicenseKey, Order, Refund, now, session

Violation = dict[str, str]
STUCK_AFTER = timedelta(minutes=30)
EMAILED_STATUSES = ("delivered", "partially_refunded")  # a delivered order must have had its access email


def _cents(x: float | None) -> int:
    return round((x or 0.0) * 100)


def _v(invariant: str, detail: str) -> Violation:
    return {"invariant": invariant, "detail": detail}


def every_paid_order_delivered() -> list[Violation]:
    """No order is left in 'paid' once the bus has been drained."""
    with session() as s:
        ids = list(s.scalars(select(Order.id).where(Order.status == "paid").order_by(Order.id)))
    return [_v("every_paid_order_delivered", f"order {i} is still 'paid'") for i in ids]


def every_delivered_order_emailed() -> list[Violation]:
    """Every delivered order's access email went out (I4): delivery and email are tracked apart."""
    with session() as s:
        ids = list(s.scalars(select(Order.id).where(Order.status.in_(EMAILED_STATUSES),
                                                    Order.email_sent.is_not(True)).order_by(Order.id)))
    return [_v("every_delivered_order_emailed", f"order {i} was delivered but never emailed") for i in ids]


def ledger_balanced() -> list[Violation]:
    """Total debits equal total credits, and so does every transaction."""
    txns: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    with session() as s:
        for txn, debit, credit in s.execute(select(LedgerEntry.txn_id, LedgerEntry.debit, LedgerEntry.credit)):
            txns[txn][0] += _cents(debit)
            txns[txn][1] += _cents(credit)
    out = [_v("ledger_balanced", f"txn {t}: debit {d} != credit {c} cents") for t, (d, c) in sorted(txns.items()) if d != c]
    debit, credit = sum(d for d, _ in txns.values()), sum(c for _, c in txns.values())
    if debit != credit:
        out.append(_v("ledger_balanced", f"ledger total: debit {debit} != credit {credit} cents"))
    return out


def no_key_on_two_orders() -> list[Violation]:
    """A licence key belongs to at most one order, and an order holds at most `quantity` keys."""
    with session() as s:
        shared = s.scalars(select(LicenseKey.key).where(LicenseKey.order_id.is_not(None)).group_by(LicenseKey.key)
                           .having(func.count(func.distinct(LicenseKey.order_id)) > 1)).all()
        held = s.execute(select(Order.id, Order.quantity, func.count(LicenseKey.id))
                         .join(LicenseKey, LicenseKey.order_id == Order.id)
                         .group_by(Order.id, Order.quantity).order_by(Order.id)).all()
    out = [_v("no_key_on_two_orders", f"key {k} is assigned to several orders") for k in shared]
    out += [_v("no_key_on_two_orders", f"order {o} holds {n} keys for quantity {q or 1}") for o, q, n in held if n > (q or 1)]
    return out


def refunds_not_above_order_amount() -> list[Violation]:
    with session() as s:
        rows = s.execute(select(Order.id, Order.amount_eur, func.sum(Refund.amount_eur))
                         .join(Refund, Refund.order_id == Order.id)
                         .group_by(Order.id, Order.amount_eur).order_by(Order.id)).all()
    return [_v("refunds_not_above_order_amount", f"order {o}: refunded {r:.2f} of {a:.2f} EUR")
            for o, a, r in rows if _cents(r) > _cents(a)]


def no_order_without_ledger_lines() -> list[Violation]:
    with session() as s:
        booked = select(LedgerEntry.ref).where(LedgerEntry.ref.is_not(None))
        ids = list(s.scalars(select(Order.id).where(Order.id.not_in(booked)).order_by(Order.id)))
    return [_v("no_order_without_ledger_lines", f"order {i} has no ledger lines") for i in ids]


def marketing_spend_within_budget() -> list[Violation]:
    """Month-to-date campaign spend stays within the ops setting marketing_budget_month_eur."""
    from aecom.company_settings import get_setting
    from aecom.policy.gate import load_policy

    budget = float(get_setting("marketing_budget_month_eur")
                   or load_policy()["company_settings_defaults"]["marketing_budget_month_eur"])
    n = datetime.now()  # same month boundary as the policy gate
    with session() as s:
        spent = s.scalar(select(func.coalesce(func.sum(Campaign.spent_eur), 0))
                         .where(Campaign.created_at >= datetime(n.year, n.month, 1))) or 0.0
    if _cents(spent) > _cents(budget):
        return [_v("marketing_spend_within_budget", f"spent {spent:.2f} of {budget:.2f} EUR this month")]
    return []


def no_run_stuck_running() -> list[Violation]:
    with session() as s:
        rows = s.execute(select(AgentRun.id, AgentRun.agent_id).where(
            AgentRun.status == "running", AgentRun.started_at < now() - STUCK_AFTER).order_by(AgentRun.id)).all()
    return [_v("no_run_stuck_running", f"run {r} of {a} has been 'running' for over 30 min") for r, a in rows]


def no_dead_letters() -> list[Violation]:
    """No dead-lettered event, except an ``order.paid`` the reconciler superseded (its order is delivered and emailed).

    Delivery is the only ``order.paid`` handler, so once a requeued event has delivered and emailed the order the
    dead copy has nothing left to do.
    """
    from aecom.bus.events import dead_letters

    with session() as s:
        dead_paid = {e.id: (e.payload or {}).get("order_id") for e in s.scalars(
            select(EventRow).where(EventRow.dead.is_(True), EventRow.topic == "order.paid"))}
        settled = set(s.scalars(select(Order.id).where(Order.id.in_(set(dead_paid.values()) - {None}),
                                                       Order.status.in_(EMAILED_STATUSES),
                                                       Order.email_sent.is_(True))))
    return [_v("no_dead_letters", f"event {d['id']} {d['topic']} dead after {d['attempts']} attempts: {d['last_error']}")
            for d in dead_letters() if dead_paid.get(d["id"]) not in settled]


CHECKS: dict[str, Callable[[], list[Violation]]] = {fn.__name__: fn for fn in (
    every_paid_order_delivered, every_delivered_order_emailed, ledger_balanced, no_key_on_two_orders,
    refunds_not_above_order_amount, no_order_without_ledger_lines, marketing_spend_within_budget, no_run_stuck_running,
    no_dead_letters)}


def check_all() -> list[Violation]:
    """Run every invariant; returns all violations (empty when the company is consistent)."""
    return [v for check in CHECKS.values() for v in check()]
