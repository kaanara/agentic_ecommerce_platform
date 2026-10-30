"""J9: month-end close report - numbers correct against a seeded ledger, VAT per country sums to vat_payable,
refuses an unfinished month, immutable versions, and the dashboard tab.

Sales, a refund, expenses and agent runs are booked through the real store/ledger APIs and backdated into a
finished month (February 2026) with `aecom.simulator._backdate` / `ledger_rewrites_allowed()`, the same technique
tests/test_metrics.py and tests/test_accountant_export.py use - the ledger is append-only (J11), so editing
`created_at` after the fact needs that context manager. Every test gets its own temp SQLite DB from
tests/conftest.py's `fresh_company` fixture (autouse).
"""

from __future__ import annotations

import csv
import io
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from aecom import incidents, ledger, metrics, month_end, periods, reports
from aecom.db import AgentRun, Campaign, LedgerEntry, Period, Refund, ledger_rewrites_allowed, now, session
from aecom.graph.agents import daily_budget
from aecom.persona.loader import agent_ids
from aecom.simulator import _backdate
from aecom.store.adapter import get_store

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partial does `from _common import ...`

FEB = "2026-02"
TODAY = datetime(2026, 3, 2)  # a fixed "now" after February has ended, for deterministic backend tests
THIS_MONTH = f"{now().year}-{now().month:02d}"  # the real wall-clock month, for the dashboard refusal test


def _at(txn: str, ts: datetime) -> None:
    with session() as s, ledger_rewrites_allowed():
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)):
            e.created_at = ts
        s.flush()


def _order(email: str, country: str, ts: datetime, campaign_id: str | None = None) -> str:
    o = get_store().create_order(email, "DL-ME-V1", country=country, campaign_id=campaign_id)
    _backdate(o["id"], ts)
    return o["id"]


@pytest.fixture
def february():
    """Two orders in February 2026 (one DE via a campaign, one FR), the DE one half-refunded, marketing/AI/vendor
    expenses, two agent runs and one open incident - all inside the month, so month_end's numbers can be checked
    against it by hand. Totals (verified against metrics.pnl/metrics.balances while writing this test):
    net revenue 125.34, refunds 42.02, cogs 20, marketing 30, ai 2, vendor 15, cash in 200 out 117 closing 83,
    VAT payable DE 7.99 (19%) + FR 16.67 (20%) = 24.66."""
    store = get_store()
    store.create_product("DL-ME-V1", "Close test", 100.0, "download", unit_cost_eur=10.0)
    store.set_status("DL-ME-V1", "active")
    with session() as s:
        s.add(Campaign(id="cmp_me", name="Feb push", channel="google_ads", status="live", budget_eur=100.0))
    de = _order("a@example.com", "DE", datetime(2026, 2, 5), "cmp_me")
    _order("b@example.com", "FR", datetime(2026, 2, 10))
    get_store().refund_order(de, 50.0, "test")
    with session() as s:
        rtxn = s.scalars(select(LedgerEntry.txn_id)
                         .where(LedgerEntry.ref == de, LedgerEntry.account == "refunds")).one()
        s.scalars(select(Refund).where(Refund.order_id == de)).one().created_at = datetime(2026, 2, 15)
    _at(rtxn, datetime(2026, 2, 15))
    _at(ledger.record_expense("marketing_expense", 30.0, "ad spend", "cmp_me"), datetime(2026, 2, 6))
    _at(ledger.record_expense("ai_expense", 2.0, "llm tokens"), datetime(2026, 2, 7))
    _at(ledger.record_expense("vendor_expense", 15.0, "hosting"), datetime(2026, 2, 8))
    with session() as s:
        s.add(AgentRun(run_id="run_me", agent_id="finance", model="scripted", task="t", status="ok", cost_eur=0.5,
                       started_at=datetime(2026, 2, 12)))
        s.add(AgentRun(run_id="run_me", agent_id="ceo", model="scripted", task="t", status="ok", cost_eur=0.3,
                       started_at=datetime(2026, 2, 12)))
    incidents.raise_incident("custom_monitor", "P2", "test_subject", "a test incident", now=datetime(2026, 2, 20))
    return de


# --------------------------------------------------------------------------- build(): numbers against the ledger
def test_build_matches_ground_truth_pnl_and_cash(february):
    data = month_end.build(FEB)
    start, end = month_end.bounds(FEB)
    pnl, balances = metrics.pnl(start, end), metrics.balances(start, end)
    assert data["period"] == {"name": FEB, "start": "2026-02-01T00:00:00", "end": "2026-03-01T00:00:00", "days": 28}
    assert data["pnl"] == {"net_revenue_eur": pnl["net_revenue"], "refunds_eur": round(balances["refunds"], 2),
                          "cogs_eur": pnl["cogs"], "marketing_expense_eur": pnl["marketing_expense"],
                          "ai_expense_eur": pnl["ai_expense"], "vendor_expense_eur": pnl["vendor_expense"],
                          "gross_profit_eur": pnl["gross_profit"], "gross_margin_pct": pnl["gross_margin_pct"],
                          "net_profit_eur": pnl["net_profit"]}
    assert pnl["net_revenue"] == 125.34 and round(balances["refunds"], 2) == 42.02
    assert data["cash"] == {"opening_eur": 0.0, "in_eur": 200.0, "out_eur": 117.0, "closing_eur": 83.0}
    assert data["cash"]["closing_eur"] == pnl["cash"]  # a fresh ledger: opening balance is zero
    assert data["refunds_count"] == 1
    assert data["top_products"] == [{"sku": "DL-ME-V1", "title": "Close test", "orders": 2,
                                     "net_revenue_eur": pnl["net_revenue"],
                                     "refunds_eur": round(balances["refunds"], 2),
                                     "refunded_orders": 1, "refund_rate_pct": 50.0}]
    inc = data["incidents"]
    assert inc["open_total"] == 1 and inc["by_severity"] == {"P1": 0, "P2": 1, "P3": 0}
    assert inc["open"][0]["monitor"] == "custom_monitor" and inc["open"][0]["severity"] == "P2"
    assert data["checks"] == {"ledger_balanced": True, "every_mirrored_order_has_ledger_lines": True,
                              "period_status": "open", "ledger_fingerprint": None}
    assert data["unmatched_items"] == [] and data["unmatched_items_count"] == 0
    assert data["payout_reconciliation"] == {"available": False,
                                             "reason": "not available until payout reconciliation (F4) is built",
                                             "open_items": []}


def test_vat_per_country_sums_to_vat_payable(february):
    data = month_end.build(FEB)
    start, end = month_end.bounds(FEB)
    by_country = {v["country"]: v for v in data["vat"]["by_country_rate"]}
    assert set(by_country) == {"DE", "FR"}
    assert by_country["DE"]["vat_rate_pct"] == 19.0 and by_country["FR"]["vat_rate_pct"] == 20.0
    for v in data["vat"]["by_country_rate"]:  # net x rate == vat, to the cent, for every country/rate group
        assert round(v["net_eur"] * v["vat_rate_pct"] / 100, 2) == pytest.approx(v["vat_eur"], abs=0.01)
    total = round(sum(v["vat_eur"] for v in data["vat"]["by_country_rate"]), 2)
    assert total == data["vat"]["total_vat_eur"] == 24.66
    assert data["vat"]["total_vat_eur"] == pytest.approx(metrics.balances(start, end)["vat_payable"], abs=0.01)
    assert data["vat"]["total_vat_eur"] == pytest.approx(metrics.pnl(start, end)["vat_payable"], abs=0.01)


def test_marketing_and_ai_budget_vs_actual(february):
    data = month_end.build(FEB)
    m = data["budget"]["marketing"]
    assert m["spent_eur"] == 30.0 and m["budget_eur"] - m["spent_eur"] == m["variance_eur"]
    ai = data["budget"]["ai"]
    by_agent = {a["agent_id"]: a for a in ai["by_agent"]}
    assert set(agent_ids()) <= set(by_agent)
    assert by_agent["finance"]["spent_eur"] == 0.5 and by_agent["ceo"]["spent_eur"] == 0.3
    assert by_agent["support"]["spent_eur"] == 0.0  # no runs booked for an agent still shows its budget
    for agent, row in by_agent.items():
        assert row["budget_eur"] == round(daily_budget(agent) * 28, 2)  # February 2026 has 28 days
        assert row["variance_eur"] == round(row["budget_eur"] - row["spent_eur"], 2)
    assert ai["total_budget_eur"] == round(sum(a["budget_eur"] for a in ai["by_agent"]), 2)
    assert ai["total_spent_eur"] == round(sum(a["spent_eur"] for a in ai["by_agent"]), 4)


def test_numbers_text_passes_number_check_and_reads_well(february):
    data = month_end.build(FEB)
    text = month_end.numbers_text(data)
    assert reports.number_check(text, data) == []
    assert "Net revenue: \u20ac125.34" in text and "Refunds: \u20ac42.02" in text
    assert "VAT payable by country and rate: DE 19.0%" in text and "FR 20.0%" in text
    assert "Total VAT payable: \u20ac24.66" in text
    assert "Top product: Close test with 2 orders" in text
    assert "Refunds booked: 1." in text and "Open incidents P1/P2/P3: 0/1/0." in text
    assert "Payout reconciliation: not available until payout reconciliation (F4) is built." in text
    assert "Checks: ledger balanced True, every mirrored order booked True, period open." in text


def test_pnl_csv_round_trips_the_pnl_section(february):
    data = month_end.build(FEB)
    rows = list(csv.reader(io.StringIO(month_end.pnl_csv(data))))
    assert rows[0] == ["key", "value"]
    values = dict(rows[1:])
    assert values == {k: str(v) for k, v in data["pnl"].items()}


# --------------------------------------------------------------------------- generate(): immutable + refusals
def test_refuses_an_unfinished_month(february):
    with pytest.raises(ValueError, match="has not ended yet"):
        month_end.generate("2026-03", "Ada", today=TODAY)  # March 2 - March itself has only just started
    with pytest.raises(ValueError, match="by \\(the human's name\\) is required"):
        month_end.generate(FEB, "", today=TODAY)
    assert reports.list_reports("month_end") == []  # neither refusal stored anything


def test_generate_is_immutable_and_versions_increase(february):
    first = month_end.generate(FEB, "Ada", today=TODAY)
    assert first["version"] == 1 and first["kind"] == "month_end" and first["created_by"] == "human:Ada"
    assert first["period_start"] == "2026-02-01T00:00:00" and first["period_end"] == "2026-03-01T00:00:00"
    assert reports.number_check(first["text"], first["data"]) == []
    second = month_end.generate(FEB, "Ada", today=TODAY)  # a second call always adds a new version, never edits
    assert second["version"] == 2 and second["id"] != first["id"]
    assert reports.get_report(first["id"]) == first
    assert [r["version"] for r in reports.list_reports("month_end")] == [2, 1]
    assert reports.report_title("month_end") == "Month-end close report"


def test_closed_period_gets_its_report_id_attached_without_touching_other_fields(february):
    closed = periods.close(FEB, "Ada")
    report = month_end.generate(FEB, "Ada", today=TODAY)
    assert report["data"]["checks"]["period_status"] == "closed"
    assert report["data"]["checks"]["ledger_fingerprint"] == closed["fingerprint"]
    with session() as s:
        row = s.get(Period, FEB)
        assert row.report_id == report["id"]
        assert (row.status, row.closed_by, row.ledger_hash) == ("closed", "Ada", closed["fingerprint"])


def test_report_id_is_not_attached_to_an_open_period():
    with session() as s:
        s.add(Period(period="2024-05"))
    month_end._attach_to_period("2024-05", "rpt_should_not_be_stored")
    with session() as s:
        assert s.get(Period, "2024-05").report_id is None


# --------------------------------------------------------------------------- dashboard tab
def _tab_named():
    from _acct_report import render_month_end_tab

    render_month_end_tab("Ada")


def _tab_blank():
    from _acct_report import render_month_end_tab

    render_month_end_tab("")


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def test_tab_shows_empty_state_with_no_ledger_activity():
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("No ledger activity yet" in m.value for m in at.markdown)


def test_tab_blocks_creating_without_a_name(february):
    at = AppTest.from_function(_tab_blank).run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert not any(b.key == "_month_end_create_btn" for b in at.button)


def test_tab_previews_and_creates_a_report(february):
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    month_box = at.selectbox(key="_month_end_select")
    assert FEB in month_box.options
    month_box.set_value(FEB)
    at = at.run()
    markdown = " ".join(m.value for m in at.markdown)
    assert "Net revenue: \u20ac125.34" in markdown
    at.button(key="_month_end_create_btn").click().run()
    assert not at.exception, _messages(at)
    assert any("created" in s.value for s in at.success)
    stored = reports.list_reports("month_end")
    assert len(stored) == 1 and stored[0]["created_by"] == "human:Ada"


def test_tab_refuses_creating_the_current_open_month():
    store = get_store()
    store.create_product("DL-ME-NOW", "Still open", 50.0, "download", unit_cost_eur=5.0)
    store.set_status("DL-ME-NOW", "active")
    store.create_order("now@example.com", "DL-ME-NOW", country="DE")  # today, i.e. inside THIS_MONTH

    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    at.selectbox(key="_month_end_select").set_value(THIS_MONTH)
    at = at.run()
    at.button(key="_month_end_create_btn").click().run()
    assert not at.exception, _messages(at)
    assert any("has not ended yet" in e.value for e in at.error)
    assert reports.list_reports("month_end") == []


def test_tab_offers_downloads_for_a_stored_report(february):
    month_end.generate(FEB, "Ada", today=TODAY)
    at = AppTest.from_function(_tab_named).run()
    at.selectbox(key="_month_end_select").set_value(FEB)
    at = at.run()
    assert not at.exception, _messages(at)
    assert len(at.download_button) == 2
    labels = {d.label for d in at.download_button}
    assert labels == {"Markdown", "P&L CSV"}
