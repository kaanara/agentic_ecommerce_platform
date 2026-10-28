"""Business invariants that must hold after any run, however many faults were injected.

Each check returns a list of violations ``{"invariant": name, "detail": text}``; an empty list means it holds.
Money is compared in integer cents so float noise never counts as a violation.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select

from aecom.db import AgentRun, AuditLog, Campaign, EventRow, LedgerEntry, LicenseKey, Order, Refund, now, session

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
    """No dead-lettered event. The reconciler revives dead ``order.paid`` events instead of copying them, so a
    repaired order leaves nothing behind in the dead letter."""
    from aecom.bus.events import dead_letters

    return [_v("no_dead_letters", f"event {d['id']} {d['topic']} dead after {d['attempts']} attempts: {d['last_error']}")
            for d in dead_letters()]


def every_mirrored_order_has_ledger_lines() -> list[Violation]:
    """A store-platform order (``external_id`` set, e.g. mirrored from Lemon Squeezy) has exactly one sale booked
    for its amount, and the cash its refund lines pay out equals its Refund rows: replays never book twice."""
    with session() as s:
        orders = s.execute(select(Order.id, Order.external_id, Order.amount_eur)
                           .where(Order.external_id.is_not(None)).order_by(Order.id)).all()
        lines = s.execute(select(LedgerEntry.ref, LedgerEntry.txn_id, LedgerEntry.memo, LedgerEntry.account,
                                 LedgerEntry.debit, LedgerEntry.credit)
                          .where(LedgerEntry.ref.in_([o.id for o in orders]))).all()
        refunded = dict(s.execute(select(Refund.order_id, func.sum(Refund.amount_eur)).group_by(Refund.order_id)).all())
    sales: dict[str, set[str]] = defaultdict(set)
    sold, paid_back = defaultdict(int), defaultdict(int)
    for ref, txn, memo, account, debit, credit in lines:
        if memo.startswith("sale ") and account == "cash" and debit:
            sales[ref].add(txn)
            sold[ref] += _cents(debit)
        elif memo.startswith("refund ") and account == "cash":
            paid_back[ref] += _cents(credit)
    out = []
    for oid, ext, amount in orders:
        if len(sales[oid]) != 1 or sold[oid] != _cents(amount):
            out.append(_v("every_mirrored_order_has_ledger_lines",
                          f"order {oid} ({ext}): {len(sales[oid])} sale txns for {sold[oid]} of {_cents(amount)} cents"))
        if paid_back[oid] != _cents(refunded.get(oid)):
            out.append(_v("every_mirrored_order_has_ledger_lines", f"order {oid} ({ext}): refund lines pay out "
                          f"{paid_back[oid]} cents, refunds total {_cents(refunded.get(oid))}"))
    return out


def refund_mirror_matches_provider(provider: dict[str, int]) -> list[Violation]:
    """Every provider order is mirrored and its local refunded total equals the provider's ``refunded_amount``
    (``provider`` maps the provider order id to that amount in cents). Not in CHECKS (the ls-* scenarios add it): it needs the provider's view."""
    with session() as s:
        local = dict(s.execute(select(Order.external_id, Order.id).where(Order.external_id.in_(list(provider)))).all())
        refunded = dict(s.execute(select(Refund.order_id, func.sum(Refund.amount_eur))
                                  .where(Refund.order_id.in_(list(local.values()))).group_by(Refund.order_id)).all())
    out = []
    for ext, want in sorted(provider.items()):
        if ext not in local:
            out.append(_v("refund_mirror_matches_provider", f"provider order {ext} is not mirrored"))
        elif _cents(refunded.get(local[ext])) != want:
            out.append(_v("refund_mirror_matches_provider", f"provider order {ext}: refunded "
                          f"{_cents(refunded.get(local[ext]))} cents locally, {want} at the provider"))
    return out


def no_unearned_promotion() -> list[Violation]:
    """Every tool above stage 1 got there by a human promotion with evidence: the latest ``autonomy_stage`` audit
    row of the tool is a ``promoted`` by ``human:<name>`` to the stage now in force. A stage written straight into
    ops_settings (or kept after a later demotion) has no such row."""
    from aecom import autonomy

    promoted = autonomy.promoted_tools()
    if not promoted:
        return []
    latest: dict[str, Any] = {}
    with session() as s:
        for row in s.scalars(select(AuditLog).where(AuditLog.action == "autonomy_stage").order_by(AuditLog.id)):
            latest[(row.detail or {}).get("tool")] = row
    out = []
    for tool, stage in sorted(promoted.items()):
        row = latest.get(tool)
        detail = (row.detail or {}) if row else {}
        if row is None or row.decision != "promoted" or not row.actor.startswith("human:") \
                or not detail.get("evidence") or detail.get("to") != stage:
            out.append(_v("no_unearned_promotion", f"{tool} is at stage {stage} without a human promotion "
                          "with evidence in the audit log"))
    return out


CHECKS: dict[str, Callable[[], list[Violation]]] = {fn.__name__: fn for fn in (
    every_paid_order_delivered, every_delivered_order_emailed, ledger_balanced, no_key_on_two_orders,
    refunds_not_above_order_amount, no_order_without_ledger_lines, marketing_spend_within_budget, no_run_stuck_running,
    no_dead_letters, every_mirrored_order_has_ledger_lines, no_unearned_promotion)}


def check_all() -> list[Violation]:
    """Run every invariant; returns all violations (empty when the company is consistent)."""
    return [v for check in CHECKS.values() for v in check()]
