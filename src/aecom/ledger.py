"""Double-entry ledger. Finance agent reads it; the store and tools write to it."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from aecom.db import LedgerEntry, new_id, session

# debit-normal accounts grow with debits; the rest grow with credits
DEBIT_NORMAL = {"cash", "cogs", "marketing_expense", "ai_expense", "vendor_expense", "refunds"}
EXPENSE_ACCOUNTS = {"cogs", "marketing_expense", "ai_expense", "vendor_expense"}


def post(lines: list[tuple[str, float, float]], memo: str, ref: str | None = None) -> str:
    """lines = [(account, debit, credit), ...]; must balance."""
    debit = round(sum(d for _, d, _ in lines), 6)
    credit = round(sum(c for _, _, c in lines), 6)
    if abs(debit - credit) > 1e-6:
        raise ValueError(f"unbalanced transaction: debit={debit} credit={credit}")
    txn = new_id("txn")
    with session() as s:
        for account, d, c in lines:
            s.add(LedgerEntry(txn_id=txn, account=account, debit=round(d, 4), credit=round(c, 4), memo=memo, ref=ref))
    return txn


def record_sale(order_id: str, gross: float, vat: float, unit_cost: float) -> str:
    lines = [("cash", gross, 0.0), ("revenue", 0.0, gross - vat), ("vat_payable", 0.0, vat)]
    if unit_cost:
        lines += [("cogs", unit_cost, 0.0), ("cash", 0.0, unit_cost)]
    return post(lines, f"sale {order_id}", order_id)


def record_refund(order_id: str, amount: float) -> str:
    return post([("refunds", amount, 0.0), ("cash", 0.0, amount)], f"refund {order_id}", order_id)


def record_expense(account: str, amount: float, memo: str, ref: str | None = None) -> str:
    if account not in EXPENSE_ACCOUNTS:
        raise ValueError(f"{account} is not an expense account")
    return post([(account, amount, 0.0), ("cash", 0.0, amount)], memo, ref)


def balances() -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    with session() as s:
        for e in s.scalars(select(LedgerEntry)):
            sign = 1 if e.account in DEBIT_NORMAL else -1
            totals[e.account] += sign * (e.debit - e.credit)
    return {k: round(v, 4) for k, v in totals.items()}


def pnl() -> dict[str, float]:
    b = balances()
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
