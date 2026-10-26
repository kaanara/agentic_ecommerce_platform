"""J3/J4: report snapshots are immutable versions and their commentary may only quote numbers from their data."""

import asyncio
import hashlib
import json
from datetime import datetime

import pytest
from sqlalchemy import func, select

from aecom import reports
from aecom.db import Report, session
from aecom.policy.gate import gate_builtin
from aecom.simulator import _backdate
from aecom.store.adapter import get_store

DATA = {"pnl": {"net_revenue": 1234.56, "gross_margin_pct": 71.4, "net_profit": -3.0},
        "kpis": {"orders": 12, "aov_eur": 49.0, "roas": 40.83, "cac_eur": None}}


@pytest.fixture
def sale():
    store = get_store()
    store.create_product("DL-RPT-V1", "Report test", 100.0, "download", unit_cost_eur=10.0)
    store.set_status("DL-RPT-V1", "active")
    store.create_order("a@example.com", "DL-RPT-V1", country="US")


@pytest.fixture
def october_sale():
    store = get_store()
    store.create_product("DL-RPT-V2", "Report test", 100.0, "download", unit_cost_eur=10.0)
    store.set_status("DL-RPT-V2", "active")
    _backdate(store.create_order("a@example.com", "DL-RPT-V2", country="US")["id"], datetime(2026, 10, 5, 12))


def test_number_check_accepts_rounded_and_formatted_matches():
    text = ("Revenue was €1,234.56 (1.234,56 in German), about 1235 overall. Margin 71% (71.4%), ROAS 40.8, "
            "AOV €49 on 12 orders, net profit -3.")
    assert reports.number_check(text, DATA) == []


def test_number_check_rejects_invented_numbers():
    assert reports.number_check("Revenue was €1,500 and margin 72%; ROAS 40.9.", DATA) == ["€1,500", "72%", "40.9"]
    assert reports.number_check("We expect 25 orders tomorrow.", DATA) == ["25"]


def test_number_check_checks_numbers_glued_to_letters():
    text = "revenue €5000k, ROAS 37x, 250EUR, 12pct, 999orders, Q4 target 4500"
    assert reports.number_check(text, {"pnl": {"net_profit": 3.0}}) == [
        "€5000k", "37x", "250EUR", "12pct", "999orders", "4500"]
    # A decimal with a suffix is checked as a whole and scaled, not truncated to the part before the point.
    assert reports.number_check("Revenue €1.2k.", {"pnl": {"net_revenue": 1.0}}) == ["€1.2k"]
    assert reports.number_check("Revenue €1.2k, 2 million, 5 percent, EUR250.", {"x": 1.0}) == [
        "€1.2k", "2 million", "5 percent", "EUR250"]
    assert reports.number_check("Revenue €1.2k, €1,234.56k? no: €1.23k, ROAS 40.8x, 71pct, 12orders.",
                                {"rev": 1234.56, "roas": 40.83, "margin": 71.4, "orders": 12}) == ["€1,234.56k"]


def test_number_check_ignores_dates_years_list_markers_and_counts():
    text = "1. Q3 2026 summary for 2026-10-25\n2) Generated 2026-10-26T07:00:00\n3 insights from 2 campaigns: 12 orders."
    assert reports.number_check(text, DATA) == []
    assert reports.number_check("Orders: 7.", DATA) == ["7"]  # a bare small number is still checked


def test_create_report_stores_immutable_versions(october_sale):
    today = datetime(2026, 10, 26)
    first = reports.create_report("daily_flash", "month_to_date", "Quiet start.", created_by="finance", today=today)
    assert first["version"] == 1 and first["created_by"] == "finance"
    assert first["data"]["pnl"]["net_revenue"] == 100.0 and first["data"]["kpis"]["orders"] == 1
    assert first["period_start"] == "2026-10-01T00:00:00" and first["data"]["period"]["days"] == 26

    second = reports.create_report("daily_flash", "month_to_date", "Net revenue €100 from 1 order.", today=today)
    assert second["version"] == 2 and second["sha256"] != first["sha256"] and second["id"] != first["id"]
    assert reports.get_report(first["id"]) == first
    assert [r["version"] for r in reports.list_reports("daily_flash")] == [2, 1]
    assert reports.latest("daily_flash")["id"] == second["id"]
    canonical = json.dumps(first["data"], sort_keys=True, separators=(",", ":"))
    assert first["sha256"] == hashlib.sha256((canonical + "Quiet start.").encode()).hexdigest()

    other = reports.create_report("weekly_review", "month_to_date", "Fine.", today=today)
    assert other["version"] == 1  # versions count per kind and period


def test_invented_number_raises_and_stores_nothing(october_sale):
    with pytest.raises(ValueError, match="report mentions numbers not in its data: €250"):
        reports.create_report("daily_flash", "month_to_date", "Net revenue €250.", today=datetime(2026, 10, 26))
    with session() as s:
        assert s.scalar(select(func.count(Report.id))) == 0


def _submit(agent_id: str, text: str) -> str:
    tool = gate_builtin("submit_report", agent_id)
    return asyncio.run(tool.ainvoke({"kind": "daily_flash", "period": "today", "text": text}))


def test_submit_report_through_the_gate(sale):
    out = json.loads(_submit("finance", "Net revenue €100 from 1 order today."))
    assert out["version"] == 1 and len(out["sha256"]) == 64
    assert reports.get_report(out["id"])["created_by"] == "finance"

    err = _submit("finance", "Net revenue €999.")
    assert err.startswith("ERROR: ValueError: report mentions numbers not in its data: €999")

    assert _submit("marketing", "Net revenue €100.").startswith("DENIED by policy")
    assert [r["created_by"] for r in reports.list_reports()] == ["finance"]
