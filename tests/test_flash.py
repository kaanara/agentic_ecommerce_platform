"""J5: the daily flash - code computes the numbers, Finance comments, and the flash always goes out."""

import inspect
from datetime import timedelta

import pytest
from aecom import db, flash, incidents, metrics, reports, scheduler
from aecom.graph.agents import AgentResult
from aecom.llm import Script
from aecom.store.adapter import get_store


@pytest.fixture
def tomorrow():
    """A reference time one day ahead, so 'yesterday' is the window holding the orders created now."""
    store = get_store()
    store.create_product("DL-FLASH-V1", "Flash test", 100.0, "download", unit_cost_eur=10.0)
    store.set_status("DL-FLASH-V1", "active")
    store.create_order("a@example.com", "DL-FLASH-V1", country="US")
    store.create_order("b@example.com", "DL-FLASH-V1", country="US")
    return db.now() + timedelta(days=1)


@pytest.fixture
def config(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(flash.secrets, "get", lambda name: values.get(name))
    return values


@pytest.fixture
def mail(monkeypatch):
    sent: list[tuple] = []

    def send(self, to, subject, text, inbox=None):
        sent.append((to, subject, text))
        return {"email_id": "eml_x"}

    monkeypatch.setattr(flash.AgentMail, "send", send)
    return sent


def test_build_flash_numbers_equal_metrics_for_yesterday(tomorrow):
    incidents.raise_incident("refund_rate", "P2", subject="DL-FLASH-V1", detail="high")
    incidents.raise_incident("bus_backlog", "P3")
    data = flash.build_flash(tomorrow)
    start, end = metrics.period("yesterday", tomorrow)
    pnl, kpis = metrics.pnl(start, end), metrics.kpis(start, end)
    assert data["metrics"] == {
        "orders": kpis["orders"], "net_revenue_eur": pnl["net_revenue"],
        "refunds_eur": round(metrics.balances(start, end).get("refunds", 0.0), 2),
        "marketing_spend_eur": pnl["marketing_expense"], "ai_cost_eur": pnl["ai_expense"],
        "ai_cost_per_order_eur": kpis["ai_cost_per_order_eur"]}
    assert data["metrics"]["orders"] == 2 and data["metrics"]["net_revenue_eur"] > 0
    assert data["incidents"]["open"] == {"P1": 0, "P2": 1, "P3": 1}
    assert data["incidents"]["titles"]["P2"] == ["refund_rate: DL-FLASH-V1"]
    assert data["approvals"] == {"pending": 0, "oldest_age_hours": None}
    assert data["flags"] == {"kill_switch": False, "agents_paused": False, "safe_mode": False}
    assert set(data["event_bus"]) == {"waiting_for_retry", "queued", "dead_letter"}


def test_flash_with_finance_commentary_is_stored_and_emailed(tomorrow, config, mail):
    config["REPORT_EMAIL"] = "owner@example.com"
    config["ALERT_EMAIL"] = "alerts@example.com"
    incidents.raise_incident("slow_delivery", "P3", subject="ord_1")
    revenue = flash.build_flash(tomorrow)["metrics"]["net_revenue_eur"]
    commentary = f"Yesterday brought 2 orders and €{revenue:.2f} net revenue. A quiet, healthy day."
    Script.load({"finance": [{"text": commentary}]})
    r = flash.send_daily_flash(tomorrow)
    assert r["emailed"] is True and r["fallback_reason"] is None
    report = reports.get_report(r["report_id"])
    assert report["kind"] == "daily_flash" and report["created_by"] == "finance" and report["text"] == commentary
    assert len(mail) == 1
    to, subject, body = mail[0]
    date = (tomorrow - timedelta(days=1)).date().isoformat()
    assert to == "owner@example.com" and subject == f"Daily flash {date}"
    assert body.startswith(commentary)
    assert "Net revenue (EUR)" in body and "Open issues (P2/P3)" in body and "[P3] slow_delivery: ord_1" in body


def test_finance_error_gives_numbers_only_flash(tomorrow, config, mail, monkeypatch):
    config["ALERT_EMAIL"] = "alerts@example.com"

    async def failing(agent_id, task, **kw):
        return AgentResult(agent_id, "boom", status="error")

    monkeypatch.setattr("aecom.graph.agents.run_agent", failing)
    r = flash.send_daily_flash(tomorrow)
    assert r["emailed"] is True and "error" in r["fallback_reason"]
    report = reports.get_report(r["report_id"])
    assert report["kind"] == "daily_flash" and report["created_by"] == "system" and "numbers only" in report["text"]
    assert [m[0] for m in mail] == ["alerts@example.com"]
    assert "Commentary unavailable" in mail[0][2] and "Orders" in mail[0][2]


def test_invented_number_in_commentary_falls_back(tomorrow, config, mail):
    config["REPORT_EMAIL"] = "owner@example.com"
    Script.load({"finance": [{"text": "Yesterday we made €98,765.43 in net revenue."}]})
    r = flash.send_daily_flash(tomorrow)
    assert "number check" in r["fallback_reason"] and r["emailed"] is True
    report = reports.get_report(r["report_id"])
    assert "98,765" not in report["text"] and report["created_by"] == "system"
    assert "98,765" not in mail[0][2]


def test_no_address_stores_report_without_email(tomorrow, config, mail):
    Script.load({"finance": [{"text": "Two orders came in yesterday."}]})
    r = flash.send_daily_flash(tomorrow)
    assert r["emailed"] is False and mail == []
    assert reports.latest("daily_flash")["id"] == r["report_id"]


def test_scheduler_job_sends_flash_and_no_longer_writes_shared_memory(monkeypatch):
    src = inspect.getsource(scheduler.daily_finance_report)
    assert "remember" not in src and "shared=true" not in src
    calls = []
    monkeypatch.setattr(flash, "send_daily_flash", lambda now=None: calls.append(now) or {})
    scheduler.daily_finance_report()
    assert calls == [None]
