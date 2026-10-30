"""J6/J12/J13: weekly business review and monthly governance report data, generated text, API, exports, scheduler."""

import ast
import csv
import io
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aecom import ledger, reports
from aecom.api import app as api_app
from aecom.db import AgentRun, Approval, AuditLog, Campaign, Incident, OutboxEmail, Suppression, ToolCallLog, session
from aecom.integrations.agentmail import AgentMail
from aecom.store.adapter import get_store

TOKEN = "test-token-reports"
OCT = datetime(2026, 10, 15, 12)
TODAY = datetime(2026, 11, 3, 9)  # last_month = October 2026


@pytest.fixture
def week():
    """Two products, one campaign with spend, one refund, support mail and agent runs, all inside last_7_days."""
    store = get_store()
    for sku, title, price in [("DL-WK-A", "Alpha guide", 100.0), ("DL-WK-B", "Beta pack", 40.0)]:
        store.create_product(sku, title, price, "download", unit_cost_eur=10.0)
        store.set_status(sku, "active")
    with session() as s:
        s.add(Campaign(id="cmp_week", name="Autumn push", channel="google_ads", status="live", budget_eur=100.0))
    a1 = store.create_order("a1@example.com", "DL-WK-A", country="US", campaign_id="cmp_week")
    store.create_order("a2@example.com", "DL-WK-A", country="US")
    store.create_order("b1@example.com", "DL-WK-B", country="US")
    store.refund_order(a1["id"], 100.0, "not as described")
    ledger.record_expense("marketing_expense", 20.0, "ad spend google_ads", "cmp_week")
    mail = AgentMail()
    mail.record_inbound("c@example.com", "Where is my key?", "help")
    mail.record_inbound("d@example.com", "Invoice", "please")
    mail.reply("msg_1", "Here you go", to="c@example.com", subject="Re: Where is my key?")
    mail.send("e@example.com", "Newsletter", "not a reply")
    with session() as s:
        for agent, cost in [("support", 0.002), ("support", 0.003), ("ceo", 0.01)]:
            s.add(AgentRun(run_id="run_w", agent_id=agent, model="scripted", task="t", status="ok", cost_eur=cost))


@pytest.fixture
def october():
    """Governance events inside October 2026, plus one in September and one in November that must be ignored."""
    sep, nov = datetime(2026, 9, 30, 23), datetime(2026, 11, 1, 1)
    with session() as s:
        s.add_all([
            Approval(agent_id="marketing", tool_name="launch_campaign", args={}, risk_tier="T2", reason="r",
                     status="executed", decided_by="kaan", created_at=OCT, decided_at=OCT),
            Approval(agent_id="finance", tool_name="book_expense", args={}, risk_tier="T3", reason="r",
                     status="rejected", decided_by="kaan", created_at=OCT, decided_at=OCT),
            Approval(agent_id="ceo", tool_name="hire_vendor", args={}, risk_tier="T3", reason="r",
                     status="rejected", decided_by="mia", created_at=sep, decided_at=OCT),
            Approval(agent_id="ceo", tool_name="x", args={}, risk_tier="T2", reason="r", status="pending", created_at=OCT),
            Approval(agent_id="ceo", tool_name="y", args={}, risk_tier="T2", reason="r", status="approved",
                     decided_by="kaan", created_at=nov, decided_at=nov),
            ToolCallLog(agent_id="marketing", tool_name="post", decision="deny", ts=OCT),
            ToolCallLog(agent_id="marketing", tool_name="post", decision="deny", ts=OCT),
            ToolCallLog(agent_id="support", tool_name="refund", decision="deny", ts=OCT),
            ToolCallLog(agent_id="support", tool_name="refund", decision="allow", ts=OCT),
            ToolCallLog(agent_id="ceo", tool_name="x", decision="deny", ts=sep),
            AuditLog(actor="human:kaan", action="kill_switch", decision="on", ts=OCT),
            AuditLog(actor="system", action="safe_mode", decision="on", detail={"reason": "P1"}, ts=OCT),
            AuditLog(actor="human:kaan", action="safe_mode", decision="off", ts=OCT),
            AuditLog(actor="human:kaan", action="agents_paused", decision="on", ts=nov),
            AuditLog(actor="system", action="marketing_opt_in", decision="executed", ts=OCT),
            AuditLog(actor="system", action="marketing_unsubscribe", decision="executed", ts=OCT),
            AuditLog(actor="system", action="marketing_unsubscribe", decision="executed", ts=OCT),
            AuditLog(actor="system", action="marketing_suppressed", decision="executed", ts=OCT),
            AuditLog(actor="system", action="provisioning", decision="activated", detail={"vendor": "lemonsqueezy"}, ts=OCT),
            AuditLog(actor="ceo", action="account_need", decision="decided", detail={"vendor": "meta"}, ts=OCT),
            Incident(monitor="heartbeat", severity="P1", first_seen=OCT, last_seen=OCT, status="resolved", resolved_at=OCT),
            Incident(monitor="undelivered_order", severity="P3", first_seen=OCT, last_seen=OCT),
            Incident(monitor="old", severity="P2", first_seen=sep, last_seen=sep, status="resolved", resolved_at=OCT),
            Suppression(email="x@example.com", reason="unsubscribe", created_at=OCT),
            Suppression(email="y@example.com", reason="bounce", created_at=nov),
        ])


def test_weekly_review_data_matches_db(week):
    d = reports.build_data("weekly_review", "last_7_days")
    assert d["pnl"]["net_revenue"] == 140.0 and d["kpis"]["orders"] == 3  # 240 sold, 100 refunded
    products = {p["sku"]: p for p in d["products"]}
    assert products["DL-WK-A"] == {"sku": "DL-WK-A", "title": "Alpha guide", "orders": 2, "net_revenue_eur": 100.0,
                                   "refunds_eur": 100.0, "refunded_orders": 1, "refund_rate_pct": 50.0}
    assert products["DL-WK-B"]["orders"] == 1 and products["DL-WK-B"]["refund_rate_pct"] == 0.0
    assert [p["sku"] for p in d["products"]] == ["DL-WK-A", "DL-WK-B"]  # by net revenue
    assert d["campaigns"] == [{"id": "cmp_week", "name": "Autumn push", "channel": "google_ads", "status": "live",
                               "spend_eur": 20.0, "net_revenue_eur": 0.0, "roas": 0.0}]  # its one order was refunded
    assert d["support"] == {"inbound_emails": 2, "replies": 1}
    assert d["ai"]["runs"] == 3 and d["ai"]["per_agent"] == [
        {"agent_id": "ceo", "runs": 1, "cost_eur": 0.01}, {"agent_id": "support", "runs": 2, "cost_eur": 0.005}]
    assert d["ai"]["cost_per_order_eur"] == d["kpis"]["ai_cost_per_order_eur"]


def test_governance_data_matches_db(october):
    g = reports.build_data("governance", "last_month", TODAY)
    assert g["period"]["start"] == "2026-10-01T00:00:00" and g["period"]["days"] == 31
    a = g["approvals"]
    assert a["created"] == 3 and a["decided"] == 3  # the September-created one was decided in October
    assert a["by_outcome"] == {"executed": 1, "rejected": 2} and a["by_decider"] == {"kaan": 2, "mia": 1}
    assert {(x["tool_name"], x["outcome"], x["decided_by"]) for x in a["decisions"]} == {
        ("launch_campaign", "executed", "kaan"), ("book_expense", "rejected", "kaan"), ("hire_vendor", "rejected", "mia")}
    assert g["policy_denies"] == {"total": 3, "by_agent": {"marketing": 2, "support": 1}}
    c = g["controls"]
    assert (c["changes"], c["kill_switch"], c["safe_mode"], c["agents_paused"]) == (3, 1, 2, 0)
    assert [(e["setting"], e["state"], e["actor"]) for e in c["events"]] == [
        ("kill_switch", "on", "human:kaan"), ("safe_mode", "on", "system"), ("safe_mode", "off", "human:kaan")]
    assert g["incidents"]["opened"] == {"P1": 1, "P2": 0, "P3": 1}
    assert g["incidents"]["resolved"] == {"P1": 1, "P2": 1, "P3": 0}
    assert g["gdpr"] == {"opt_ins": 1, "unsubscribes": 2, "suppressions": 1, "suppressions_by_reason": {"unsubscribe": 1}}
    assert g["provisioning"]["events"] == 2
    assert {(p["action"], p["vendor"]) for p in g["provisioning"]["log"]} == {("provisioning", "lemonsqueezy"),
                                                                              ("account_need", "meta")}


def test_generated_text_passes_number_check_and_is_versioned(week):
    first = reports.generate("weekly_review", "last_7_days")
    assert first["created_by"] == "system" and first["version"] == 1
    assert reports.number_check(first["text"], first["data"]) == []
    for line in ["Orders: 3. Net revenue: €140.00.", "Top product: Alpha guide with 2 orders",
                 "Highest refund rate: Alpha guide at 50.0%", "Support: 2 inbound emails, 1 replies.",
                 "Highest AI cost: ceo at €0.0100 (1 runs)."]:
        assert line in first["text"]
    second = reports.generate("weekly_review", "last_7_days")
    assert second["version"] == 2 and reports.get_report(first["id"]) == first
    with pytest.raises(ValueError, match="numbers not in its data"):
        reports.generate("weekly_review", "last_7_days", text="Revenue was €999.")


def test_governance_text_and_lines_with_unknown_numbers_are_dropped(october):
    r = reports.generate("governance", "last_month", today=TODAY)
    assert reports.number_check(r["text"], r["data"]) == []
    assert "Approvals: 3 created, 3 decided (1 executed, 2 rejected)." in r["text"]
    assert "Policy denies: 3 (marketing 2, support 1)." in r["text"]
    assert "Incidents opened P1/P2/P3: 1/0/1; resolved: 1/1/0." in r["text"]
    assert "GDPR: 1 opt-ins, 2 unsubscribes, 1 suppressions." in r["text"]
    # A campaign name with a figure that isn't in the data can't make the summary fail the check.
    data = reports.build_data("weekly_review", "last_7_days", TODAY)
    data["campaigns"] = [{"id": "c", "name": "Promo 77", "channel": "x", "status": "live", "spend_eur": 5.0,
                          "net_revenue_eur": 0.0, "roas": 0.0}]
    text = reports.summary(data)
    assert "Promo" not in text and reports.number_check(text, data) == []


def test_csv_and_markdown_exports(week):
    r = reports.generate("weekly_review", "last_7_days")
    rows = list(csv.reader(io.StringIO(reports.to_csv(r))))
    assert rows[0] == ["key", "value"]
    values = dict(rows[1:])
    assert len(values) == len(rows) - 1  # keys are unique
    assert values["kind"] == "weekly_review" and values["pnl.net_revenue"] == "140.0"
    assert values["products.0.sku"] == "DL-WK-A" and values["products.0.refund_rate_pct"] == "50.0"
    assert values["support.replies"] == "1" and values["ai.per_agent.1.agent_id"] == "support"
    assert values["campaigns.0.roas"] == "0.0"
    assert reports.flatten({"a": {"b": None, "c": [True, 2]}}) == [("a.b", None), ("a.c.0", True), ("a.c.1", 2)]
    assert reports.to_csv({"data": {"x": None, "y": False}}) == "key,value\nx,\ny,false\n"
    md = reports.to_markdown(r)
    assert md.startswith("# Weekly business review v1\n") and r["text"] in md and r["sha256"] in md
    assert "| pnl.net_revenue | 140.0 |" in md and "| products.1.title | Beta pack |" in md


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_app.secrets, "get", lambda name: TOKEN if name == "AECOM_API_TOKEN" else None)
    return TestClient(api_app.app)


def test_report_api_needs_token_and_returns_stored_report(week, client):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    weekly = reports.generate("weekly_review", "last_7_days")
    reports.generate("weekly_review", "last_7_days")
    flash = reports.generate("daily_flash", "yesterday")
    assert client.get("/reports").status_code == 401
    assert client.get(f"/reports/{weekly['id']}").status_code == 401
    assert client.get(f"/reports/{weekly['id']}", headers={"Authorization": "Bearer nope"}).status_code == 401

    listed = client.get("/reports", params={"kind": "weekly_review"}, headers=auth).json()
    assert [r["version"] for r in listed] == [2, 1] and all(r["kind"] == "weekly_review" for r in listed)
    assert len(client.get("/reports", params={"limit": 1}, headers=auth).json()) == 1
    assert {r["id"] for r in client.get("/reports", headers=auth).json()} >= {weekly["id"], flash["id"]}
    got = client.get(f"/reports/{weekly['id']}", headers=auth).json()
    assert got == weekly
    assert {"data", "text", "sha256", "version"} <= set(got)
    assert client.get("/reports/rpt_missing", headers=auth).status_code == 404


def test_send_report_emails_markdown_or_only_stores(week, monkeypatch):
    import aecom.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod.secrets, "get", lambda name: None)
    out = reports.send_report("weekly_review", "last_7_days")
    assert out["emailed"] is False and reports.get_report(out["report_id"])["kind"] == "weekly_review"

    monkeypatch.setattr(secrets_mod.secrets, "get", lambda name: "owner@example.com" if name == "ALERT_EMAIL" else None)
    out = reports.send_report("governance", "last_month")
    assert out["emailed"] is True
    with session() as s:
        sent = s.scalars(select(OutboxEmail).where(OutboxEmail.to == "owner@example.com")).one()
        assert sent.subject.startswith("Governance report ") and "## Data" in sent.body


def test_reports_page_requires_login_first():
    page = Path(__file__).resolve().parents[1] / "dashboard" / "app_pages" / "reports.py"
    tree = ast.parse(page.read_text())
    for stmt in tree.body:
        if isinstance(stmt, ast.Import | ast.ImportFrom):
            continue
        call = stmt.value if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) else None
        name = ast.unparse(call.func) if call else None
        if name == "require_login":
            break
        assert name == "st.set_page_config", f"{name or ast.unparse(stmt)} runs before require_login()"
    else:
        pytest.fail("5_Reports.py never calls require_login()")
    src = page.read_text()
    assert "download_button" in src and "to_csv" in src and "to_markdown" in src


def test_scheduler_registers_weekly_and_governance_report_jobs(monkeypatch):
    from aecom import scheduler

    sent = []
    monkeypatch.setattr(reports, "send_report", lambda kind, period, today=None: sent.append((kind, period)) or {})
    jobs = {j.id: j for j in scheduler.build_scheduler(demo=False).get_jobs()}
    assert {"weekly_review", "weekly_review_report", "governance_report", "finance_daily"} <= set(jobs)
    weekly = {f.name: str(f) for f in jobs["weekly_review_report"].trigger.fields}
    assert (weekly["day_of_week"], weekly["hour"], weekly["minute"]) == ("mon", "8", "0")
    gov = {f.name: str(f) for f in jobs["governance_report"].trigger.fields}
    assert (gov["day"], gov["hour"], gov["minute"]) == ("1", "7", "30")
    jobs["weekly_review_report"].func()
    jobs["governance_report"].func()
    assert sent == [("weekly_review", "last_7_days"), ("governance", "last_month")]
    demo = {j.id for j in scheduler.build_scheduler(demo=True).get_jobs()}
    assert {"weekly_review_report", "governance_report"} <= demo
