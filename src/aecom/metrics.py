"""Period-aware P&L and KPIs. Money always comes from the ledger so the two can never disagree.

Periods are half-open [start, end) in naive UTC (like db.now()); None means unbounded on that side.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from aecom.db import Customer, LedgerEntry, Order, now, session
from aecom.ledger import DEBIT_NORMAL, EXPENSE_ACCOUNTS

PERIODS = ("today", "yesterday", "last_7_days", "month_to_date", "last_month", "quarter_to_date")


def _in(col: Any, start: datetime | None, end: datetime | None) -> list[Any]:
    conds = []
    if start is not None:
        conds.append(col >= start)
    if end is not None:
        conds.append(col < end)
    return conds


def period(name: str, today: date | datetime | None = None) -> tuple[datetime, datetime]:
    """(start, end) for a named period; windows end at the next midnight so today is included."""
    ref = today or now()
    d = ref.date() if isinstance(ref, datetime) else ref
    midnight = datetime(d.year, d.month, d.day)
    tomorrow = midnight + timedelta(days=1)
    month = datetime(d.year, d.month, 1)
    if name == "today":
        return midnight, tomorrow
    if name == "yesterday":
        return midnight - timedelta(days=1), midnight
    if name == "last_7_days":  # today and the six days before it
        return midnight - timedelta(days=6), tomorrow
    if name == "month_to_date":
        return month, tomorrow
    if name == "last_month":
        prev = month - timedelta(days=1)
        return datetime(prev.year, prev.month, 1), month
    if name == "quarter_to_date":
        return datetime(d.year, 3 * ((d.month - 1) // 3) + 1, 1), tomorrow
    raise ValueError(f"unknown period {name!r}; expected one of {', '.join(PERIODS)}")


def balances(start: datetime | None = None, end: datetime | None = None) -> dict[str, float]:
    """Account movements in the period (signed like ledger.balances())."""
    totals: dict[str, float] = defaultdict(float)
    with session() as s:
        for e in s.scalars(select(LedgerEntry).where(*_in(LedgerEntry.created_at, start, end))):
            sign = 1 if e.account in DEBIT_NORMAL else -1
            totals[e.account] += sign * (e.debit - e.credit)
    return {k: round(v, 4) for k, v in totals.items()}


def pnl(start: datetime | None = None, end: datetime | None = None) -> dict[str, float]:
    """Profit & loss for the period; same keys as ledger.pnl() and equal to it when unbounded."""
    b = balances(start, end)
    revenue = b.get("revenue", 0.0) - b.get("refunds", 0.0)
    expenses = {a: b.get(a, 0.0) for a in sorted(EXPENSE_ACCOUNTS)}
    gross_profit = revenue - expenses["cogs"]
    net = revenue - sum(expenses.values())
    return {
        "net_revenue": round(revenue, 2),
        **{k: round(v, 4) for k, v in expenses.items()},
        "gross_profit": round(gross_profit, 2),
        "gross_margin_pct": round(100 * gross_profit / revenue, 1) if revenue else 0.0,
        "net_profit": round(net, 2),
        "cash": b.get("cash", 0.0),
        "vat_payable": b.get("vat_payable", 0.0),
    }


def _campaign_revenue(start: datetime | None, end: datetime | None) -> float:
    """Net revenue minus refunds booked in the period against campaign-attributed orders."""
    with session() as s:
        rows = s.execute(
            select(LedgerEntry.account, func.sum(LedgerEntry.credit - LedgerEntry.debit))
            .join(Order, Order.id == LedgerEntry.ref)
            .where(Order.campaign_id.is_not(None), LedgerEntry.account.in_(["revenue", "refunds"]),
                   *_in(LedgerEntry.created_at, start, end))
            .group_by(LedgerEntry.account)
        ).all()
    return sum(v or 0.0 for _, v in rows)  # refunds are debit-normal, so they arrive negative


def kpis(start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
    """Business KPIs for the period. Revenue and costs from the ledger; counts from orders placed in the period."""
    p = pnl(start, end)
    revenue = p["net_revenue"]
    spend = p["marketing_expense"]
    placed = [Order.status != "failed", *_in(Order.created_at, start, end)]
    with session() as s:
        orders = s.scalar(select(func.count(Order.id)).where(*placed)) or 0
        per_customer = select(Order.customer_id, func.count(Order.id).label("n")).where(*placed).group_by(Order.customer_id).subquery()
        customers = s.scalar(select(func.count()).select_from(per_customer)) or 0
        repeaters = s.scalar(select(func.count()).select_from(per_customer).where(per_customer.c.n > 1)) or 0
        first = (select(Order.customer_id, func.min(Order.created_at).label("first_at"))
                 .where(Order.status != "failed").group_by(Order.customer_id).subquery())
        new_paid = s.scalar(select(func.count(Customer.id)).join(first, first.c.customer_id == Customer.id)
                            .where(Customer.acquisition_channel.is_not(None), *_in(first.c.first_at, start, end))) or 0
    camp_rev = _campaign_revenue(start, end)
    return {
        "orders": orders, "net_revenue_eur": revenue, "aov_eur": round(revenue / orders, 2) if orders else 0,
        "customers": customers, "repeat_rate_pct": round(100 * repeaters / customers, 1) if customers else 0,
        "ltv_eur": round(revenue / customers, 2) if customers else 0, "cac_eur": round(spend / new_paid, 2) if new_paid else None,
        "roas": round(camp_rev / spend, 2) if spend else None, "ai_cost_eur": p["ai_expense"],
        "ai_cost_per_order_eur": round(p["ai_expense"] / orders, 3) if orders else None,
        "gross_margin_pct": p["gross_margin_pct"],
    }
