"""J3/J4: immutable report snapshots. Code computes the numbers (data), agents only write the commentary (text).

A report is never edited: a correction is a new row with the next version for the same kind and period.
The number check rejects commentary that quotes a figure which isn't in the report's data.

J6/J12: kinds ``weekly_review`` and ``governance`` add sections computed from the database; ``generate`` stores them
with a deterministic numbers-only summary when no agent commentary is given. J13: ``to_markdown`` / ``to_csv`` export
a stored report; ``send_report`` generates one and emails it like the daily flash.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from aecom import metrics
from aecom.db import (AgentRun, Approval, AuditLog, Campaign, Incident, LedgerEntry, Order, OutboxEmail, Product, Refund,
                      Report, Suppression, ToolCallLog, session)

log = logging.getLogger(__name__)

_KIND = re.compile(r"[a-z][a-z0-9_]{0,39}")
# Stripped before extraction: ISO dates/timestamps and list markers at the start of a line ("1." / "2)").
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\b")
_LIST_MARKER = re.compile(r"^(\s*)\d{1,3}[.)](?=\s)", re.MULTILINE)
# A number plus whatever is glued to it. Scale suffixes (k, M, mn, mio, bn, "million"; a lowercase "m" is
# ambiguous with minutes/metres, so "5m" is just the number 5) multiply the value; any other letters
# ("37x", "250EUR", "999orders") still leave the number checked. Never skip a number because letters follow it.
_NUMBER = re.compile(
    r"(?<![\w.,])(?P<sign>[-−])?(?P<cur>(?:[€$£]|(?i:eur|usd|gbp)(?![a-z]))\s?)?(?P<num>\d+(?:[.,]\d+)*)"
    r"(?P<scale>(?:[kK]|M|(?i:mn|mio|b|bn))(?![A-Za-z])|\s?(?i:thousand|million|billion)\b)?"
    r"(?P<pct>\s?%|\s?(?i:pct|percent)\b)?(?P<suffix>[A-Za-z]+)?"
)
_SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mio": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_COUNT_WORD = re.compile(r"[\s-]+[A-Za-z]")


def _between(col: Any, start: datetime, end: datetime) -> list[Any]:
    return [col >= start, col < end]


def _order_ledger(group: Any, start: datetime, end: datetime, *conds: Any) -> dict[Any, dict[str, float]]:
    """{group value: {"revenue": net, "refunds": net}} for ledger lines booked in the period against orders."""
    out: dict[Any, dict[str, float]] = {}
    with session() as s:
        rows = s.execute(select(group, LedgerEntry.account, func.sum(LedgerEntry.credit - LedgerEntry.debit))
                         .join(Order, Order.id == LedgerEntry.ref)
                         .where(LedgerEntry.account.in_(["revenue", "refunds"]), *conds,
                                *_between(LedgerEntry.created_at, start, end))
                         .group_by(group, LedgerEntry.account)).all()
    for key, account, value in rows:
        out.setdefault(key, {"revenue": 0.0, "refunds": 0.0})[account] = abs(value or 0.0)
    return out


def _products(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Per product: orders placed, net revenue and refunds booked, refund rate (refunded orders / orders placed)."""
    money = _order_ledger(Order.product_id, start, end)
    with session() as s:
        placed = dict(s.execute(select(Order.product_id, func.count(Order.id))
                                .where(Order.status != "failed", *_between(Order.created_at, start, end))
                                .group_by(Order.product_id)).all())
        refunded = dict(s.execute(select(Order.product_id, func.count(func.distinct(Refund.order_id)))
                                  .join(Order, Order.id == Refund.order_id)
                                  .where(*_between(Refund.created_at, start, end)).group_by(Order.product_id)).all())
        products = list(s.execute(select(Product.id, Product.sku, Product.title)
                                  .where(Product.id.in_(set(placed) | set(refunded) | set(money)))))
    out = []
    for pid, sku, title in products:
        orders, refunds = placed.get(pid, 0), refunded.get(pid, 0)
        m = money.get(pid, {"revenue": 0.0, "refunds": 0.0})
        out.append({"sku": sku, "title": title, "orders": orders,
                    "net_revenue_eur": round(m["revenue"] - m["refunds"], 2), "refunds_eur": round(m["refunds"], 2),
                    "refunded_orders": refunds, "refund_rate_pct": round(100 * refunds / orders, 1) if orders else 0.0})
    return sorted(out, key=lambda p: (-p["net_revenue_eur"], p["sku"]))


def _campaigns(start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Per campaign: ad spend booked in the period and the net revenue of its orders booked in the period."""
    money = _order_ledger(Order.campaign_id, start, end, Order.campaign_id.is_not(None))
    with session() as s:
        spend = dict(s.execute(select(LedgerEntry.ref, func.sum(LedgerEntry.debit - LedgerEntry.credit))
                               .where(LedgerEntry.account == "marketing_expense", LedgerEntry.ref.is_not(None),
                                      *_between(LedgerEntry.created_at, start, end))
                               .group_by(LedgerEntry.ref)).all())
        campaigns = list(s.execute(select(Campaign.id, Campaign.name, Campaign.channel, Campaign.status)
                                   .where(Campaign.id.in_(set(spend) | set(money)))))
    out = []
    for cid, name, channel, status in campaigns:
        m = money.get(cid, {"revenue": 0.0, "refunds": 0.0})
        spent, revenue = round(spend.get(cid) or 0.0, 2), round(m["revenue"] - m["refunds"], 2)
        out.append({"id": cid, "name": name, "channel": channel, "status": status, "spend_eur": spent,
                    "net_revenue_eur": revenue, "roas": round(revenue / spent, 2) if spent else None})
    return sorted(out, key=lambda c: (-c["spend_eur"], c["id"]))


def _support(start: datetime, end: datetime) -> dict[str, int]:
    """Inbound emails received and threaded replies ("Re: ...") sent in the period."""
    def count(s: Any, *conds: Any) -> int:
        return s.scalar(select(func.count(OutboxEmail.id)).where(*conds, *_between(OutboxEmail.created_at, start, end))) or 0

    with session() as s:
        return {"inbound_emails": count(s, OutboxEmail.direction == "in"),
                "replies": count(s, OutboxEmail.direction == "out", func.lower(OutboxEmail.subject).like("re:%"))}


def _ai(start: datetime, end: datetime, kpis: dict[str, Any]) -> dict[str, Any]:
    """AI cost per agent from the runs started in the period; total and per order from the ledger (metrics.kpis)."""
    with session() as s:
        rows = s.execute(select(AgentRun.agent_id, func.count(AgentRun.id), func.sum(AgentRun.cost_eur))
                         .where(*_between(AgentRun.started_at, start, end)).group_by(AgentRun.agent_id)).all()
    per_agent = sorted(({"agent_id": a, "runs": n, "cost_eur": round(c or 0.0, 4)} for a, n, c in rows),
                       key=lambda r: (-r["cost_eur"], r["agent_id"]))
    return {"cost_eur": kpis["ai_cost_eur"], "cost_per_order_eur": kpis["ai_cost_per_order_eur"],
            "runs": sum(r["runs"] for r in per_agent), "per_agent": per_agent}


CONTROL_ACTIONS = ("kill_switch", "agents_paused", "safe_mode")
GDPR_ACTIONS = {"marketing_opt_in": "opt_ins", "marketing_unsubscribe": "unsubscribes",
                "marketing_suppressed": "suppressions"}
PROVISIONING_ACTIONS = ("provisioning", "account_need")


def _audit_rows(start: datetime, end: datetime, actions: Any) -> list[dict[str, Any]]:
    with session() as s:
        return [{"ts": r.ts.isoformat(), "actor": r.actor, "action": r.action, "decision": r.decision,
                 "detail": r.detail or {}}
                for r in s.scalars(select(AuditLog).where(AuditLog.action.in_(list(actions)),
                                                          *_between(AuditLog.ts, start, end))
                                   .order_by(AuditLog.ts, AuditLog.id))]


def _governance(start: datetime, end: datetime) -> dict[str, Any]:
    from aecom.incidents import SEVERITIES

    with session() as s:
        created = s.scalar(select(func.count(Approval.id)).where(*_between(Approval.created_at, start, end))) or 0
        decided = [{"id": a.id, "agent_id": a.agent_id, "tool_name": a.tool_name, "risk_tier": a.risk_tier,
                    "outcome": a.status, "decided_by": a.decided_by or "", "decided_at": a.decided_at.isoformat()}
                   for a in s.scalars(select(Approval)
                                      .where(Approval.status != "pending", Approval.decided_at.is_not(None),
                                             *_between(Approval.decided_at, start, end))
                                      .order_by(Approval.decided_at, Approval.id))]
        denies = dict(s.execute(select(ToolCallLog.agent_id, func.count(ToolCallLog.id))
                                .where(ToolCallLog.decision == "deny", *_between(ToolCallLog.ts, start, end))
                                .group_by(ToolCallLog.agent_id)).all())
        opened = Counter(s.scalars(select(Incident.severity).where(*_between(Incident.first_seen, start, end))))
        resolved = Counter(s.scalars(select(Incident.severity).where(Incident.resolved_at.is_not(None),
                                                                     *_between(Incident.resolved_at, start, end))))
        suppressed = Counter(s.scalars(select(Suppression.reason).where(*_between(Suppression.created_at, start, end))))
    controls = [{"ts": r["ts"], "actor": r["actor"], "setting": r["action"], "state": r["decision"]}
                for r in _audit_rows(start, end, CONTROL_ACTIONS)]
    gdpr = Counter(GDPR_ACTIONS[r["action"]] for r in _audit_rows(start, end, GDPR_ACTIONS))
    provisioning = [{"ts": r["ts"], "actor": r["actor"], "action": r["action"], "decision": r["decision"],
                     "vendor": str(r["detail"].get("vendor") or "")}
                    for r in _audit_rows(start, end, PROVISIONING_ACTIONS)]
    return {
        "approvals": {"created": created, "decided": len(decided),
                      "by_outcome": dict(sorted(Counter(d["outcome"] for d in decided).items())),
                      "by_decider": dict(sorted(Counter(d["decided_by"] for d in decided).items())),
                      "decisions": decided},
        "policy_denies": {"total": sum(denies.values()), "by_agent": dict(sorted(denies.items()))},
        "controls": {"changes": len(controls), **{a: sum(c["setting"] == a for c in controls) for a in CONTROL_ACTIONS},
                     "events": controls},
        "incidents": {"opened": {sev: opened.get(sev, 0) for sev in SEVERITIES},
                      "resolved": {sev: resolved.get(sev, 0) for sev in SEVERITIES},
                      "opened_total": sum(opened.values()), "resolved_total": sum(resolved.values())},
        "gdpr": {**{k: gdpr.get(k, 0) for k in GDPR_ACTIONS.values()},
                 "suppressions_by_reason": dict(sorted(suppressed.items()))},
        "provisioning": {"events": len(provisioning),
                         "by_decision": dict(sorted(Counter(f"{p['action']}:{p['decision']}" for p in provisioning).items())),
                         "log": provisioning},
    }


def build_data(kind: str, period_name: str, today: date | datetime | None = None) -> dict[str, Any]:
    """The report's numbers: P&L and KPIs from aecom.metrics for a named period, plus the kind's own sections
    (``weekly_review``: products, campaigns, support, ai; ``governance``: approvals, policy_denies, controls,
    incidents, gdpr, provisioning). Everything is computed by code."""
    start, end = metrics.period(period_name, today)
    data: dict[str, Any] = {
        "kind": kind,
        "period": {"name": period_name, "start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days},
        "pnl": metrics.pnl(start, end),
        "kpis": metrics.kpis(start, end),
    }
    if kind == "weekly_review":
        data.update(products=_products(start, end), campaigns=_campaigns(start, end), support=_support(start, end),
                    ai=_ai(start, end, data["kpis"]))
    elif kind == "governance":
        data.update(_governance(start, end))
    return data


def _candidates(num: str) -> list[tuple[float, int]]:
    """(value, decimals written) readings of '1,234.56', '1.234,56', '12,5', '40.8'. Ambiguous '1.234' gives both."""
    if "." in num and "," in num:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
        whole, frac = num.rsplit(dec, 1)
        return [(float(re.sub(r"[.,]", "", whole) + "." + frac), len(frac))]
    sep = "." if "." in num else "," if "," in num else None
    if sep is None:
        return [(float(num), 0)]
    parts = num.split(sep)
    if len(parts) > 2:  # 1,234,567 / 1.234.567
        return [(float("".join(parts)), 0)]
    whole, frac = parts
    decimal = (float(f"{whole}.{frac}"), len(frac))
    return [decimal, (float(whole + frac), 0)] if len(frac) == 3 else [decimal]


def _flatten(data: Any) -> list[float]:
    if isinstance(data, bool) or data is None:
        return []
    if isinstance(data, (int, float)):
        return [float(data)]
    if isinstance(data, dict):
        return [v for x in data.values() for v in _flatten(x)]
    if isinstance(data, (list, tuple)):
        return [v for x in data for v in _flatten(x)]
    return []


def _matches(value: float, decimals: int, known: list[float], scale: float = 1.0) -> bool:
    tolerance = (0.5 * 10 ** -decimals + 1e-9) * scale  # "40.8" matches 40.83, "71%" matches 71.4, "€1.2k" 1180
    return any(abs(abs(k) - value * scale) <= tolerance for k in known)


def number_check(text: str, data: dict[str, Any]) -> list[str]:
    """Numbers quoted in text that don't appear in data (after rounding to the precision written)."""
    cleaned = _LIST_MARKER.sub(r"\1", _DATE.sub(" ", text))
    known = _flatten(data)
    unknown: list[str] = []
    for m in _NUMBER.finditer(cleaned):
        num = m["num"]
        plain = not (m["cur"] or m["pct"] or m["sign"] or m["scale"] or m["suffix"])
        scale = _SCALES[m["scale"].strip().lower()] if m["scale"] else 1.0
        if plain and num.isdigit():
            value = int(num)
            if len(num) == 4 and 1900 <= value <= 2100:
                continue  # a year
            if value <= 10 and _COUNT_WORD.match(cleaned, m.end()):
                continue  # "3 insights", "2 campaigns"
        if not any(_matches(v, d, known, scale) for v, d in _candidates(num)):
            unknown.append(m.group(0).strip())
    return unknown


def _sha(data: dict[str, Any], text: str) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((canonical + text).encode()).hexdigest()


def _to_dict(r: Report) -> dict[str, Any]:
    return {"id": r.id, "kind": r.kind, "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None, "version": r.version, "data": r.data,
            "text": r.text, "sha256": r.sha256, "created_by": r.created_by, "created_at": r.created_at.isoformat()}


def create_report(kind: str, period_name: str, text: str, created_by: str = "system",
                  today: date | datetime | None = None, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Snapshot a new report version. Raises ValueError if the text quotes numbers that aren't in the data.
    ``data`` is for callers that already ran ``build_data(kind, period_name, today)`` (``generate``)."""
    if not _KIND.fullmatch(kind or ""):
        raise ValueError(f"invalid report kind {kind!r}; use lowercase snake_case, e.g. daily_flash or weekly_review")
    if not (text or "").strip():
        raise ValueError("report text is empty")
    data = data if data is not None else build_data(kind, period_name, today)
    if unknown := number_check(text, data):
        raise ValueError(f"report mentions numbers not in its data: {', '.join(unknown)}. "
                         "Quote only figures from get_pnl/get_kpis for this period.")
    start, end = metrics.period(period_name, today)
    with session() as s:
        previous = s.scalar(select(func.max(Report.version))
                            .where(Report.kind == kind, Report.period_start == start, Report.period_end == end)) or 0
        r = Report(kind=kind, period_start=start, period_end=end, version=previous + 1, data=data, text=text,
                   sha256=_sha(data, text), created_by=created_by)
        s.add(r)
        s.flush()
        return _to_dict(r)


def get_report(report_id: str) -> dict[str, Any] | None:
    with session() as s:
        r = s.get(Report, report_id)
        return _to_dict(r) if r else None


def list_reports(kind: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Newest first."""
    with session() as s:
        q = select(Report).order_by(Report.created_at.desc(), Report.version.desc())
        if kind is not None:
            q = q.where(Report.kind == kind)
        if limit is not None:
            q = q.limit(limit)
        return [_to_dict(r) for r in s.scalars(q)]


def latest(kind: str) -> dict[str, Any] | None:
    reports = list_reports(kind, limit=1)
    return reports[0] if reports else None


TITLES = {"daily_flash": "Daily flash", "weekly_review": "Weekly business review", "governance": "Governance report"}


def report_title(kind: str) -> str:
    return TITLES.get(kind, kind.replace("_", " ").capitalize())


def _weekly_lines(d: dict[str, Any]) -> list[str]:
    p, k, ai, sup = d["pnl"], d["kpis"], d["ai"], d["support"]
    lines = [f"Orders: {k['orders']}. Net revenue: €{p['net_revenue']:.2f}. Gross margin: {p['gross_margin_pct']:.1f}%. "
             f"Net profit: €{p['net_profit']:.2f}.",
             f"Marketing spend: €{p['marketing_expense']:.2f}" + (f", ROAS {k['roas']:.2f}." if k["roas"] is not None else ".")]
    if products := d["products"]:
        top = products[0]
        lines.append(f"Top product: {top['title']} with {top['orders']} orders and €{top['net_revenue_eur']:.2f} net revenue.")
        worst = max(products, key=lambda x: x["refund_rate_pct"])
        if worst["refund_rate_pct"]:
            lines.append(f"Highest refund rate: {worst['title']} at {worst['refund_rate_pct']:.1f}% "
                         f"({worst['refunded_orders']} of {worst['orders']} orders).")
    if ranked := [c for c in d["campaigns"] if c["roas"] is not None]:
        best = max(ranked, key=lambda c: c["roas"])
        lines.append(f"Best campaign: {best['name']}, ROAS {best['roas']:.2f} on €{best['spend_eur']:.2f} spend.")
    lines.append(f"Support: {sup['inbound_emails']} inbound emails, {sup['replies']} replies.")
    per_order = ai["cost_per_order_eur"]
    lines.append(f"AI cost: €{ai['cost_eur']:.4f}" + (f", €{per_order:.3f} per order" if per_order is not None else "")
                 + f", {ai['runs']} agent runs.")
    if ai["per_agent"]:
        top_agent = ai["per_agent"][0]
        lines.append(f"Highest AI cost: {top_agent['agent_id']} at €{top_agent['cost_eur']:.4f} ({top_agent['runs']} runs).")
    return lines


def _governance_lines(d: dict[str, Any]) -> list[str]:
    a, den, c, inc, g = d["approvals"], d["policy_denies"], d["controls"], d["incidents"], d["gdpr"]
    outcomes = ", ".join(f"{n} {o}" for o, n in a["by_outcome"].items())
    lines = [f"Approvals: {a['created']} created, {a['decided']} decided" + (f" ({outcomes})." if outcomes else ".")]
    if a["by_decider"]:
        lines.append("Decided by: " + ", ".join(f"{who or 'unknown'} ({n})" for who, n in a["by_decider"].items()) + ".")
    by_agent = ", ".join(f"{agent} {n}" for agent, n in den["by_agent"].items())
    lines.append(f"Policy denies: {den['total']}" + (f" ({by_agent})." if by_agent else "."))
    lines.append(f"Control changes: {c['changes']} (kill switch {c['kill_switch']}, agents paused {c['agents_paused']}, "
                 f"safe mode {c['safe_mode']}).")
    o, r = inc["opened"], inc["resolved"]
    lines.append(f"Incidents opened P1/P2/P3: {o['P1']}/{o['P2']}/{o['P3']}; resolved: {r['P1']}/{r['P2']}/{r['P3']}.")
    lines.append(f"GDPR: {g['opt_ins']} opt-ins, {g['unsubscribes']} unsubscribes, {g['suppressions']} suppressions.")
    lines.append(f"Provisioning and account events: {d['provisioning']['events']}.")
    return lines


def summary(data: dict[str, Any]) -> str:
    """Deterministic numbers-only text for a report's data. Lines whose figures fail the number check (a product or
    campaign name containing digits, say) are dropped, so the result always passes ``number_check(text, data)``."""
    kind, per = data["kind"], data["period"]
    last = (datetime.fromisoformat(per["end"]) - timedelta(days=1)).date().isoformat()
    header = f"{report_title(kind)} for {per['start'][:10]} to {last} (numbers only, no commentary)."
    if kind == "weekly_review":
        body = _weekly_lines(data)
    elif kind == "governance":
        body = _governance_lines(data)
    else:
        p, k = data["pnl"], data["kpis"]
        body = [f"Orders: {k['orders']}. Net revenue: €{p['net_revenue']:.2f}. Net profit: €{p['net_profit']:.2f}."]
    return "\n".join(line for line in [header, *body] if not number_check(line, data))


def generate(kind: str, period_name: str, text: str | None = None, created_by: str = "system",
             today: date | datetime | None = None) -> dict[str, Any]:
    """Compute and store a new version of a report. Without ``text`` the deterministic ``summary`` is the text;
    agent commentary goes through ``submit_report`` (or ``text=``) and must pass the number check."""
    data = build_data(kind, period_name, today)
    return create_report(kind, period_name, text if text is not None else summary(data), created_by, today, data=data)


def flatten(data: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """[(dotted key, scalar)] for nested data; list items are keyed by index (``products.0.sku``)."""
    if isinstance(data, dict):
        return [row for k, v in data.items() for row in flatten(v, f"{prefix}.{k}" if prefix else str(k))]
    if isinstance(data, (list, tuple)):
        return [row for i, v in enumerate(data) for row in flatten(v, f"{prefix}.{i}" if prefix else str(i))]
    return [(prefix, data)]


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def to_csv(report: dict[str, Any]) -> str:
    """The report's data as CSV rows ``key,value`` (flattened, see ``flatten``)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["key", "value"])
    w.writerows((k, _cell(v)) for k, v in flatten(report["data"]))
    return buf.getvalue()


def to_markdown(report: dict[str, Any]) -> str:
    """Title, provenance, the text and a key/value table of the data."""
    def esc(v: Any) -> str:
        return _cell(v).replace("|", "\\|").replace("\n", " ")

    head = [f"# {report_title(report['kind'])} v{report['version']}", "",
            f"Period: {report['period_start']} to {report['period_end']} · created by {report['created_by']} at "
            f"{report['created_at']} · sha256 `{report['sha256']}`", "", report["text"], "", "## Data", "",
            "| Key | Value |", "|---|---|"]
    return "\n".join(head + [f"| {esc(k)} | {esc(v)} |" for k, v in flatten(report["data"])]) + "\n"


def send_report(kind: str, period_name: str, today: date | datetime | None = None) -> dict[str, Any]:
    """Generate a numbers-only report version and email it (Markdown) to REPORT_EMAIL, else ALERT_EMAIL.
    Without an address it's only stored. Returns {report_id, emailed}."""
    from aecom.integrations.agentmail import AgentMail
    from aecom.observability import mask, record_error
    from aecom.secrets import secrets

    report = generate(kind, period_name, today=today)
    emailed = False
    if to := secrets.get("REPORT_EMAIL") or secrets.get("ALERT_EMAIL"):
        try:
            AgentMail().send(to, f"{report_title(kind)} {report['period_start'][:10]}", mask(to_markdown(report)))
            emailed = True
        except Exception as exc:
            log.warning("%s email failed: %s", kind, mask(str(exc)))
            record_error("reports", exc, context={"step": "email", "report_id": report["id"]})
    return {"report_id": report["id"], "emailed": emailed}
