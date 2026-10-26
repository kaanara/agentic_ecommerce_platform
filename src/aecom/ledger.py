"""Double-entry ledger. Finance agent reads it; the store and tools write to it."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select

from aecom.db import LedgerEntry, new_id, session
from aecom.money import cents, quantize, to_decimal, vat_split

# debit-normal accounts grow with debits; the rest grow with credits
DEBIT_NORMAL = {"cash", "cogs", "marketing_expense", "ai_expense", "vendor_expense", "refunds"}
EXPENSE_ACCOUNTS = {"cogs", "marketing_expense", "ai_expense", "vendor_expense"}


def post(lines: list[tuple], memo: str, ref: str | None = None) -> str:
    """lines = [(account, debit, credit), ...] or (account, debit, credit, country, vat_rate) for VAT lines.

    Balance is checked in integer cents and amounts are stored rounded to cents.
    """
    debit = sum(cents(line[1]) for line in lines)
    credit = sum(cents(line[2]) for line in lines)
    if debit != credit:
        raise ValueError(f"unbalanced transaction: debit={debit / 100:.2f} credit={credit / 100:.2f}")
    txn = new_id("txn")
    with session() as s:
        for account, d, c, *detail in lines:
            country, rate = (detail + [None, None])[:2]
            s.add(LedgerEntry(txn_id=txn, account=account, debit=float(quantize(d)), credit=float(quantize(c)),
                              memo=memo, ref=ref, country=country, vat_rate=rate))
    return txn


def record_sale(order_id: str, gross: float, vat: float, unit_cost: float,
                country: str | None = None, vat_rate: float | None = None) -> str:
    g, v, cost = quantize(gross), quantize(vat), quantize(unit_cost)
    lines: list[tuple] = [("cash", g, 0), ("revenue", 0, g - v), ("vat_payable", 0, v, country, vat_rate)]
    if cost:
        lines += [("cogs", cost, 0), ("cash", 0, cost)]
    return post(lines, f"sale {order_id}", order_id)


def _refund_basis(order_id: str) -> tuple[str | None, float | None, Decimal, Decimal]:
    """(country, vat_rate) of the order's sale VAT line, plus gross and VAT already refunded on it."""
    country = rate = None
    gross = vat = Decimal(0)
    with session() as s:
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order_id)):
            if e.memo.startswith("sale ") and e.account == "vat_payable" and e.credit:
                country, rate = e.country, e.vat_rate
            elif e.memo.startswith("refund "):
                if e.account == "cash":
                    gross += to_decimal(e.credit)
                elif e.account == "vat_payable":
                    vat += to_decimal(e.debit)
    return country, rate, gross, vat


def record_refund(order_id: str, amount: float, country: str | None = None, vat_rate: float | None = None) -> str:
    """Reverse a (partial) sale: debit refunds (net) and vat_payable (VAT), credit cash (gross).

    VAT is split on the cumulative refunded gross so partial refunds that add up to the order
    reverse exactly the VAT booked on the sale. Without a rate the sale's VAT line supplies it.
    """
    sale_country, sale_rate, prior_gross, prior_vat = _refund_basis(order_id)
    country = country or sale_country
    rate = vat_rate if vat_rate is not None else (sale_rate or 0.0)
    g = quantize(amount)
    vat = vat_split(prior_gross + g, rate)[1] - prior_vat if rate else Decimal(0)
    lines: list[tuple] = [("refunds", g - vat, 0), ("cash", 0, g)]
    if vat:
        lines.append(("vat_payable", vat, 0, country, rate))
    return post(lines, f"refund {order_id}", order_id)


def record_expense(account: str, amount: float, memo: str, ref: str | None = None) -> str:
    if account not in EXPENSE_ACCOUNTS:
        raise ValueError(f"{account} is not an expense account")
    a = quantize(amount)
    return post([(account, a, 0), ("cash", 0, a)], memo, ref)


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
