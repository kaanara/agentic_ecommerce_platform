"""J9: month-end close report for one calendar month (``YYYY-MM``).

P&L, cash movement, VAT per country and rate (OSS), budget vs actual (marketing and AI), top products, refunds,
open incidents, unmatched ledger items and payout reconciliation (F4: platform payouts ingested and reconciled
through ``aecom.payouts``, counted by the month they were received). Numbers are code-computed exactly like every
other report (``aecom.reports``); period status and its ledger fingerprint come from ``aecom.periods`` (J8) when
the month has been closed.

    month_end.build(period)            # the data dict, for any month (past, current or future-but-started)
    month_end.numbers_text(data)       # deterministic numbers-only markdown text
    month_end.generate(period, by)     # stores a new immutable version through reports.create_report; refuses
                                        # a month that has not ended yet

Optional LLM commentary is not required - numbers-only is fine - but if a caller adds it, it must go through the
same ``reports.number_check`` as every other report kind.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from aecom import incidents, metrics, periods, reports
from aecom.chaos.invariants import every_mirrored_order_has_ledger_lines, ledger_balanced, no_order_without_ledger_lines
from aecom.company_settings import get_setting
from aecom.db import AgentRun, LedgerEntry, Payout, Period, Refund, now, session
from aecom.graph.agents import daily_budget
from aecom.persona.loader import agent_ids
from aecom.policy.gate import load_policy
from aecom.reports import _between, _products

KIND = "month_end"


def bounds(period: str) -> tuple[datetime, datetime]:
    """(start, end) half-open ``[start, end)`` for the accounting month, e.g. ``'2026-01'`` ->
    ``(2026-01-01, 2026-02-01)``. Raises ValueError for anything that isn't ``YYYY-MM``.

    Delegates to ``aecom.periods`` (J8) so month boundaries are computed in exactly one place."""
    return periods._month_bounds(period)  # noqa: SLF001 - the one place this parsing may happen is shared with J8


def _cash(start: datetime, end: datetime) -> dict[str, float]:
    """Opening/closing balance of the cash account plus the period's gross in/out movement."""
    with session() as s:
        opening = s.scalar(select(func.sum(LedgerEntry.debit - LedgerEntry.credit))
                           .where(LedgerEntry.account == "cash", LedgerEntry.created_at < start)) or 0.0
        cash_in = s.scalar(select(func.sum(LedgerEntry.debit))
                           .where(LedgerEntry.account == "cash", *_between(LedgerEntry.created_at, start, end))) or 0.0
        cash_out = s.scalar(select(func.sum(LedgerEntry.credit))
                            .where(LedgerEntry.account == "cash", *_between(LedgerEntry.created_at, start, end))) or 0.0
    opening, cash_in, cash_out = round(opening, 2), round(cash_in, 2), round(cash_out, 2)
    return {"opening_eur": opening, "in_eur": cash_in, "out_eur": cash_out, "closing_eur": round(opening + cash_in - cash_out, 2)}


def _vat(start: datetime, end: datetime) -> dict[str, Any]:
    """VAT payable booked in the period, per country and rate (J7/OSS): net taxable amount and VAT, from the
    ``vat_payable`` ledger lines' ``country``/``vat_rate`` columns."""
    with session() as s:
        rows = s.execute(select(LedgerEntry.country, LedgerEntry.vat_rate,
                                func.sum(LedgerEntry.credit - LedgerEntry.debit))
                         .where(LedgerEntry.account == "vat_payable", *_between(LedgerEntry.created_at, start, end))
                         .group_by(LedgerEntry.country, LedgerEntry.vat_rate)).all()
    by_country_rate = []
    for country, rate, value in rows:
        vat_eur, rate = round(value or 0.0, 2), rate or 0.0
        by_country_rate.append({"country": country or "unknown", "vat_rate_pct": round(rate * 100, 1),
                                "net_eur": round(vat_eur / rate, 2) if rate else 0.0, "vat_eur": vat_eur})
    by_country_rate.sort(key=lambda r: (r["country"], r["vat_rate_pct"]))
    return {"by_country_rate": by_country_rate, "total_vat_eur": round(sum(r["vat_eur"] for r in by_country_rate), 2)}


def _marketing_budget(spent: float) -> dict[str, float]:
    """This month's marketing budget (the ``marketing_budget_month_eur`` company setting, same source the policy
    gate and the ``marketing_spend_within_budget`` invariant use) against what the ledger actually booked."""
    policy = load_policy()
    budget = float(get_setting("marketing_budget_month_eur")
                   or policy["company_settings_defaults"]["marketing_budget_month_eur"])
    return {"budget_eur": round(budget, 2), "spent_eur": round(spent, 2), "variance_eur": round(budget - spent, 2)}


def _ai_budget(start: datetime, end: datetime, days: int) -> dict[str, Any]:
    """Each agent's daily budget (``aecom.graph.agents.daily_budget``) times the month's length against its
    actual LLM run cost booked in the period (``ops_agent_runs``, same grouping as the weekly review's AI section)."""
    with session() as s:
        rows = s.execute(select(AgentRun.agent_id, func.sum(AgentRun.cost_eur))
                         .where(*_between(AgentRun.started_at, start, end)).group_by(AgentRun.agent_id)).all()
    spent = {agent: round(cost or 0.0, 4) for agent, cost in rows}
    by_agent = []
    for agent in sorted(set(agent_ids()) | set(spent)):
        budget = round(daily_budget(agent) * days, 2)
        actual = spent.get(agent, 0.0)
        by_agent.append({"agent_id": agent, "budget_eur": budget, "spent_eur": actual,
                         "variance_eur": round(budget - actual, 2)})
    return {"by_agent": by_agent, "total_budget_eur": round(sum(a["budget_eur"] for a in by_agent), 2),
            "total_spent_eur": round(sum(a["spent_eur"] for a in by_agent), 4)}


def _dedupe(violations: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out = []
    for v in violations:
        key = (v["invariant"], v["detail"])
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _checks(period: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Ledger balanced, every mirrored order booked, and (J8) the period's close status and - once closed - its
    ledger fingerprint, so a closed month's report can later prove its ledger still matches. Unmatched items are
    the deduplicated violations behind the two ledger checks."""
    unmatched = _dedupe(ledger_balanced() + every_mirrored_order_has_ledger_lines() + no_order_without_ledger_lines())
    period_status = periods.status(period)
    fingerprint = periods.ledger_fingerprint(period) if period_status == "closed" else None
    return {"ledger_balanced": not any(v["invariant"] == "ledger_balanced" for v in unmatched),
            "every_mirrored_order_has_ledger_lines": not any(
                v["invariant"] == "every_mirrored_order_has_ledger_lines" for v in unmatched),
            "period_status": period_status, "ledger_fingerprint": fingerprint}, unmatched


def _payouts(start: datetime, end: datetime) -> dict[str, Any]:
    """F4: platform payouts received in the month (``ops_payouts.created_at``, same ingestion-time convention
    every other section of this report uses): totals and how many have been reconciled (``aecom.payouts``)."""
    with session() as s:
        rows = list(s.scalars(select(Payout).where(*_between(Payout.created_at, start, end)).order_by(Payout.id)))
    by_status = {st: sum(1 for p in rows if p.status == st) for st in ("matched", "mismatch", "pending")}
    open_items = [{"id": p.id, "provider": p.provider, "external_id": p.external_id, "status": p.status,
                  "difference_eur": round(p.difference_eur, 2)} for p in rows if p.status != "matched"]
    return {"count": len(rows), "gross_eur": round(sum(p.gross_eur for p in rows), 2),
            "fees_eur": round(sum(p.fees_eur for p in rows), 2),
            "refunds_eur": round(sum(p.refunds_eur for p in rows), 2),
            "net_eur": round(sum(p.net_eur for p in rows), 2),
            "matched": by_status["matched"], "mismatch": by_status["mismatch"], "pending": by_status["pending"],
            "open": open_items}


def build(period: str) -> dict[str, Any]:
    """Everything in the month-end report, computed by code. Works for any month that has ledger lines yet
    (including the current, still-open one) - only ``generate`` refuses to store one that hasn't ended."""
    start, end = bounds(period)
    days = (end - start).days
    pnl, balances = metrics.pnl(start, end), metrics.balances(start, end)
    spend = pnl["marketing_expense"]
    with session() as s:
        refunds_count = s.scalar(select(func.count(Refund.id)).where(*_between(Refund.created_at, start, end))) or 0
    open_incidents = [i for i in incidents.list_incidents() if i["status"] in incidents.ACTIVE]
    by_severity = {sev: sum(1 for i in open_incidents if i["severity"] == sev) for sev in incidents.SEVERITIES}
    checks, unmatched = _checks(period)
    return {
        "kind": KIND,
        "period": {"name": period, "start": start.isoformat(), "end": end.isoformat(), "days": days},
        "pnl": {"net_revenue_eur": pnl["net_revenue"], "refunds_eur": round(balances.get("refunds", 0.0), 2),
               "cogs_eur": pnl["cogs"], "marketing_expense_eur": pnl["marketing_expense"],
               "ai_expense_eur": pnl["ai_expense"], "vendor_expense_eur": pnl["vendor_expense"],
               "gross_profit_eur": pnl["gross_profit"], "gross_margin_pct": pnl["gross_margin_pct"],
               "net_profit_eur": pnl["net_profit"]},
        "cash": _cash(start, end),
        "vat": _vat(start, end),
        "budget": {"marketing": _marketing_budget(spend), "ai": _ai_budget(start, end, days)},
        "top_products": _products(start, end),
        "refunds_count": refunds_count,
        "incidents": {"open_total": len(open_incidents), "by_severity": by_severity,
                     "open": [{"id": i["id"], "monitor": i["monitor"], "severity": i["severity"],
                              "subject": i["subject"]} for i in open_incidents]},
        "unmatched_items": unmatched,
        "unmatched_items_count": len(unmatched),
        "payout_reconciliation": _payouts(start, end),
        "checks": checks,
    }


def _month_name(data: dict[str, Any]) -> str:
    return datetime.fromisoformat(data["period"]["start"]).strftime("%B %Y")


def numbers_text(data: dict[str, Any]) -> str:
    """Deterministic numbers-only text, built the same way ``reports.summary`` and ``flash._numbers_only`` are:
    every figure is formatted from ``data``, and any line that would still fail the number check is dropped, so
    storing it through ``reports.create_report`` never fails."""
    per, p, cash, vat, budget = data["period"], data["pnl"], data["cash"], data["vat"], data["budget"]
    last = (datetime.fromisoformat(per["end"]) - timedelta(days=1)).date().isoformat()
    lines = [f"{reports.report_title(KIND)} for {_month_name(data)} ({per['start'][:10]} to {last}, numbers only, "
            "no commentary).",
            f"Net revenue: \u20ac{p['net_revenue_eur']:.2f}. Refunds: \u20ac{p['refunds_eur']:.2f}. "
            f"COGS: \u20ac{p['cogs_eur']:.2f}. Marketing: \u20ac{p['marketing_expense_eur']:.2f}. "
            f"AI: \u20ac{p['ai_expense_eur']:.4f}. Vendor: \u20ac{p['vendor_expense_eur']:.2f}. "
            f"Net result: \u20ac{p['net_profit_eur']:.2f}.",
            f"Cash: opening \u20ac{cash['opening_eur']:.2f}, in \u20ac{cash['in_eur']:.2f}, "
            f"out \u20ac{cash['out_eur']:.2f}, closing \u20ac{cash['closing_eur']:.2f}."]
    if vat["by_country_rate"]:
        parts = "; ".join(f"{v['country']} {v['vat_rate_pct']:.1f}%: net \u20ac{v['net_eur']:.2f}, "
                          f"VAT \u20ac{v['vat_eur']:.2f}" for v in vat["by_country_rate"])
        lines.append(f"VAT payable by country and rate: {parts}. Total VAT payable: \u20ac{vat['total_vat_eur']:.2f}.")
    else:
        lines.append("VAT payable: nothing booked this month.")
    m, ai = budget["marketing"], budget["ai"]
    lines.append(f"Marketing budget: \u20ac{m['budget_eur']:.2f}, spent \u20ac{m['spent_eur']:.2f}, "
                f"variance \u20ac{m['variance_eur']:.2f}.")
    lines.append(f"AI budget: \u20ac{ai['total_budget_eur']:.2f}, spent \u20ac{ai['total_spent_eur']:.4f}.")
    if data["top_products"]:
        top = data["top_products"][0]
        lines.append(f"Top product: {top['title']} with {top['orders']} orders and "
                    f"\u20ac{top['net_revenue_eur']:.2f} net revenue.")
    lines.append(f"Refunds booked: {data['refunds_count']}.")
    inc = data["incidents"]["by_severity"]
    lines.append(f"Open incidents P1/P2/P3: {inc['P1']}/{inc['P2']}/{inc['P3']}.")
    lines.append(f"Unmatched ledger items: {data['unmatched_items_count']}.")
    pr = data["payout_reconciliation"]
    if pr["count"]:
        lines.append(f"Payouts received: {pr['count']}, gross \u20ac{pr['gross_eur']:.2f}, "
                    f"fees \u20ac{pr['fees_eur']:.2f}, refunds \u20ac{pr['refunds_eur']:.2f}, "
                    f"net \u20ac{pr['net_eur']:.2f}; {pr['matched']} matched, {pr['mismatch']} mismatch, "
                    f"{pr['pending']} pending.")
    else:
        lines.append("Payouts received: none this month.")
    c = data["checks"]
    lines.append(f"Checks: ledger balanced {c['ledger_balanced']}, every mirrored order booked "
                f"{c['every_mirrored_order_has_ledger_lines']}, period {c['period_status']}"
                + (", fingerprint recorded." if c["ledger_fingerprint"] else "."))
    return "\n".join(line for line in lines if not reports.number_check(line, data))


def _attach_to_period(period: str, report_id: str) -> None:
    """If ``period`` is already closed in ``ops_periods`` (J8), record its month-end report id there. Never
    touches any other field on that row - closing/reopening stays entirely ``aecom.periods``'s business."""
    with session() as s:
        row = s.get(Period, period)
        if row is not None and row.status == "closed":
            row.report_id = report_id


def generate(period: str, by: str, today: date | datetime | None = None) -> dict[str, Any]:
    """Compute and store a new month-end report version through ``reports.create_report`` (immutable: a second
    call for the same month always adds the next version, it never edits or replaces one). Refuses a month that
    has not fully ended yet. ``created_by`` is ``human:<by>`` - a human always generates this one, there is no
    agent tool for it."""
    if not (by or "").strip():
        raise ValueError("by (the human's name) is required to create a month-end report")
    start, end = bounds(period)
    ref = today or now()
    ref = datetime(ref.year, ref.month, ref.day) if not isinstance(ref, datetime) else ref
    if ref < end:
        raise ValueError(f"{start.strftime('%B %Y')} has not ended yet (it runs through "
                         f"{(end - timedelta(days=1)).date().isoformat()}); a month-end close report can only be "
                         "created once the month is fully over.")
    data = build(period)
    text = numbers_text(data)
    report = reports.create_report(KIND, period, text, created_by=f"human:{by}", today=today, data=data)
    _attach_to_period(period, report["id"])
    return report


def pnl_csv(data: dict[str, Any]) -> str:
    """The month's P&L as CSV ``key,value`` rows - the dashboard's P&L-only download."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["key", "value"])
    w.writerows((k, v) for k, v in data["pnl"].items())
    return buf.getvalue()
