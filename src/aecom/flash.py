"""J5: the daily flash - yesterday's numbers, open issues and system flags, emailed to the owner every morning.

Code computes every figure (`build_flash`); Finance only adds 3-5 sentences of commentary. The flash always goes out:
if Finance is disabled, fails, or its text fails the number check, a numbers-only report is stored and emailed instead.
The "Open issues" digest of P2/P3 incidents in the email is the P3 alert digest (P3 has no channel of its own).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from aecom import incidents, metrics, reports
from aecom.approvals import list_approvals
from aecom.bus.events import status as bus_status
from aecom.company_settings import agent_enabled, agents_paused_on, get_setting, kill_switch_on
from aecom.db import now as db_now
from aecom.integrations.agentmail import AgentMail
from aecom.observability import mask, record_error
from aecom.secrets import secrets

log = logging.getLogger(__name__)

KIND, PERIOD, AGENT = "daily_flash", "yesterday", "finance"
DIGEST_SEVERITIES = ("P2", "P3")  # P1s alert on their own; P2s already emailed once, repeated here as a reminder


def _title(i: dict[str, Any]) -> str:
    return f"{i['monitor']}: {i['subject']}" if i.get("subject") else i["monitor"]


def build_flash(now: datetime | None = None) -> dict[str, Any]:
    """Everything in the flash, computed by code: yesterday's metrics, open incidents, approvals, bus and flags."""
    ref = now or db_now()
    start, end = metrics.period(PERIOD, ref)
    pnl, kpis = metrics.pnl(start, end), metrics.kpis(start, end)
    active = [i for i in incidents.list_incidents() if i["status"] in incidents.ACTIVE]
    by_sev = {sev: [_title(i) for i in active if i["severity"] == sev] for sev in incidents.SEVERITIES}
    pending = list_approvals("pending")
    oldest = min((a["created_at"] for a in pending), default=None)
    return {
        "date": start.date().isoformat(),
        "period": {"name": PERIOD, "start": start.isoformat(), "end": end.isoformat()},
        "metrics": {
            "orders": kpis["orders"],
            "net_revenue_eur": pnl["net_revenue"],
            "refunds_eur": round(metrics.balances(start, end).get("refunds", 0.0), 2),
            "marketing_spend_eur": pnl["marketing_expense"],
            "ai_cost_eur": pnl["ai_expense"],
            "ai_cost_per_order_eur": kpis["ai_cost_per_order_eur"],
        },
        "incidents": {"open": {sev: len(titles) for sev, titles in by_sev.items()}, "titles": by_sev},
        "approvals": {"pending": len(pending),
                      "oldest_age_hours": round((ref - oldest).total_seconds() / 3600, 1) if oldest else None},
        "event_bus": bus_status(),
        "flags": {"kill_switch": kill_switch_on(), "agents_paused": agents_paused_on(),
                  "safe_mode": get_setting("safe_mode", "off") == "on"},
    }


def _numbers_only(data: dict[str, Any], now: datetime | None) -> str:
    """Deterministic summary. Only lines whose figures pass the report's number check are kept, so storing never fails."""
    m = data["metrics"]
    per_order = m["ai_cost_per_order_eur"]
    lines = [f"Daily flash for {data['date']} (numbers only, no commentary).",
             f"Orders: {m['orders']}.",
             f"Net revenue: €{m['net_revenue_eur']:.2f}.",
             f"Marketing spend: €{m['marketing_spend_eur']:.2f}.",
             f"AI cost: €{m['ai_cost_eur']:.4f}" + (f", €{per_order:.3f} per order." if per_order is not None else ".")]
    known = reports.build_data(KIND, PERIOD, now)
    return "\n".join(line for line in lines if not reports.number_check(line, known))


def _commentary(data: dict[str, Any], now: datetime | None) -> tuple[str | None, str | None]:
    """(text, None) from Finance, or (None, reason) if there is no usable commentary."""
    if kill_switch_on():
        return None, "kill switch is on"
    if agents_paused_on():
        return None, "agents are paused"
    if not agent_enabled(AGENT):
        return None, f"{AGENT} is disabled"
    from aecom.graph.agents import run_agent

    task = ("Daily flash commentary. Write 3-5 plain sentences on yesterday's business for the owner, using only the "
            "numbers in this JSON (rounding is fine). Reply with the commentary as your final answer; don't call "
            "submit_report or remember, code stores and emails the flash.\n\n" + json.dumps(data, default=str))
    try:
        r = asyncio.run(run_agent(AGENT, task))
    except Exception as exc:
        record_error("flash", exc, context={"step": "commentary"})
        return None, f"{AGENT} failed: {type(exc).__name__}"
    text = (r.output or "").strip()
    if r.status != "ok":
        return None, f"{AGENT} run status {r.status}"
    if not text:
        return None, f"{AGENT} returned no text"
    if unknown := reports.number_check(text, reports.build_data(KIND, PERIOD, now)):
        return None, f"number check rejected the commentary ({len(unknown)} figure(s) not in the data)"
    return text, None


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):  # cents, but keep sub-euro AI costs readable
        return f"{v:.2f}" if v == 0 or abs(v) >= 1 else f"{v:.4f}"
    return str(v)


def _table(data: dict[str, Any]) -> str:
    m, inc, apr = data["metrics"], data["incidents"]["open"], data["approvals"]
    rows = [("Orders", m["orders"]), ("Net revenue (EUR)", m["net_revenue_eur"]), ("Refunds (EUR)", m["refunds_eur"]),
            ("Marketing spend (EUR)", m["marketing_spend_eur"]), ("AI cost (EUR)", m["ai_cost_eur"]),
            ("AI cost per order (EUR)", m["ai_cost_per_order_eur"]),
            ("Open incidents P1/P2/P3", f"{inc['P1']}/{inc['P2']}/{inc['P3']}"),
            ("Pending approvals", apr["pending"]), ("Oldest approval (hours)", apr["oldest_age_hours"]),
            *((f"Event bus {k.replace('_', ' ')}", v) for k, v in data["event_bus"].items()),
            *((f"{k.replace('_', ' ').capitalize()}", v) for k, v in data["flags"].items())]
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"{label.ljust(width)}  {_fmt(value)}" for label, value in rows)


def _digest(data: dict[str, Any]) -> str:
    titles = data["incidents"]["titles"]
    lines = [f"- [{sev}] {t}" for sev in DIGEST_SEVERITIES for t in titles[sev]]
    return "\n".join(lines) if lines else "None."


def _body(commentary: str, data: dict[str, Any], fallback_reason: str | None) -> str:
    parts = [commentary]
    if fallback_reason:
        parts.append(f"(Commentary unavailable: {fallback_reason}.)")
    parts += ["", f"Numbers for {data['date']}", _table(data), "", "Open issues (P2/P3)", _digest(data)]
    return mask("\n".join(parts))


def send_daily_flash(now: datetime | None = None) -> dict[str, Any]:
    """Build, store and email the daily flash. Never skips the flash; returns {report_id, emailed, fallback_reason}."""
    data = build_flash(now)
    text, fallback_reason = _commentary(data, now)
    report = None
    if text is not None:
        try:
            report = reports.create_report(KIND, PERIOD, text, created_by=AGENT, today=now)
        except ValueError as exc:
            text, fallback_reason = None, f"report rejected: {exc}"
    if report is None:
        text = _numbers_only(data, now)
        report = reports.create_report(KIND, PERIOD, text, created_by="system", today=now)
    emailed = False
    to = secrets.get("REPORT_EMAIL") or secrets.get("ALERT_EMAIL")
    if to:
        try:
            AgentMail().send(to, f"Daily flash {data['date']}", _body(text, data, fallback_reason))
            emailed = True
        except Exception as exc:
            log.warning("daily flash email failed: %s", mask(str(exc)))
            record_error("flash", exc, context={"step": "email", "report_id": report["id"]})
    if fallback_reason:
        log.info("daily flash %s is numbers-only: %s", report["id"], fallback_reason)
    return {"report_id": report["id"], "emailed": emailed, "fallback_reason": fallback_reason}
