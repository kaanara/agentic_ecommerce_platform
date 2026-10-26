from datetime import date, datetime

import pytest
from sqlalchemy import select

from aecom import ledger, metrics
from aecom.db import LedgerEntry, session
from aecom.simulator import _backdate
from aecom.store.adapter import get_store, vat_included

TODAY = date(2026, 10, 15)


def _at(txn: str, ts: datetime) -> None:
    with session() as s:
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)):
            e.created_at = ts


def _order(email: str, country: str, ts: datetime, campaign_id: str | None = None) -> str:
    o = get_store().create_order(email, "DL-METRICS-V1", country=country, campaign_id=campaign_id)
    _backdate(o["id"], ts)
    return o["id"]


@pytest.fixture
def two_months():
    """Sept: two US orders (one via campaign) + ad spend. Oct: repeat order on the 1st 00:00, a DE order refunded by 50, AI cost."""
    store = get_store()
    store.create_product("DL-METRICS-V1", "Metrics test", 100.0, "download", unit_cost_eur=10.0)
    store.set_status("DL-METRICS-V1", "active")
    _order("a@example.com", "US", datetime(2026, 9, 10), "cmp_1")
    _order("b@example.com", "US", datetime(2026, 9, 10))
    _at(ledger.record_expense("marketing_expense", 20.0, "ad spend", "cmp_1"), datetime(2026, 9, 12))
    _order("a@example.com", "US", datetime(2026, 10, 1), "cmp_1")
    de = _order("c@example.com", "DE", datetime(2026, 10, 5))
    _at(ledger.record_expense("ai_expense", 5.0, "llm tokens"), datetime(2026, 10, 7))
    return de


def _refund(order_id: str) -> None:
    get_store().refund_order(order_id, 50.0, "test")
    with session() as s:
        txn = s.scalars(select(LedgerEntry.txn_id).where(LedgerEntry.ref == order_id, LedgerEntry.account == "refunds")).one()
    _at(txn, datetime(2026, 10, 6))


def test_period_helpers():
    assert metrics.period("today", TODAY) == (datetime(2026, 10, 15), datetime(2026, 10, 16))
    assert metrics.period("yesterday", datetime(2026, 10, 15, 13, 30)) == (datetime(2026, 10, 14), datetime(2026, 10, 15))
    assert metrics.period("last_7_days", TODAY) == (datetime(2026, 10, 9), datetime(2026, 10, 16))
    assert metrics.period("month_to_date", TODAY) == (datetime(2026, 10, 1), datetime(2026, 10, 16))
    assert metrics.period("last_month", TODAY) == (datetime(2026, 9, 1), datetime(2026, 10, 1))
    assert metrics.period("last_month", date(2026, 1, 10)) == (datetime(2025, 12, 1), datetime(2026, 1, 1))
    assert metrics.period("quarter_to_date", TODAY) == (datetime(2026, 10, 1), datetime(2026, 10, 16))
    assert metrics.period("quarter_to_date", date(2026, 8, 31)) == (datetime(2026, 7, 1), datetime(2026, 9, 1))
    with pytest.raises(ValueError):
        metrics.period("forever", TODAY)


def test_all_time_pnl_equals_ledger(two_months):
    _refund(two_months)
    assert metrics.pnl() == metrics.pnl(None, None) == ledger.pnl()
    k = metrics.kpis()
    assert k["orders"] == 4 and k["customers"] == 3 and k["repeat_rate_pct"] == 33.3
    assert k["net_revenue_eur"] == ledger.pnl()["net_revenue"]


def test_last_month_is_filtered_and_half_open(two_months):
    p = metrics.pnl(*metrics.period("last_month", TODAY))
    assert p["net_revenue"] == 200.0 and p["cogs"] == 20.0 and p["marketing_expense"] == 20.0 and p["ai_expense"] == 0.0
    assert p["gross_profit"] == 180.0 and p["gross_margin_pct"] == 90.0 and p["net_profit"] == 160.0
    k = metrics.kpis(*metrics.period("last_month", TODAY))
    assert k["orders"] == 2 and k["customers"] == 2 and k["repeat_rate_pct"] == 0
    assert k["aov_eur"] == 100.0 and k["cac_eur"] == 20.0 and k["roas"] == 5.0  # a@ acquired via cmp_1 in Sept


def test_refunds_reduce_revenue(two_months):
    start, end = metrics.period("month_to_date", TODAY)
    net_de = 100.0 - vat_included(100.0, "DE")
    assert metrics.pnl(start, end)["net_revenue"] == pytest.approx(100.0 + net_de)
    _refund(two_months)
    p, k = metrics.pnl(start, end), metrics.kpis(start, end)
    assert p["net_revenue"] == k["net_revenue_eur"] == pytest.approx(100.0 + net_de - 50.0)
    assert k["orders"] == 2 and k["aov_eur"] == pytest.approx((50.0 + net_de) / 2, abs=0.01)
    assert k["ai_cost_eur"] == 5.0 and k["ai_cost_per_order_eur"] == 2.5
    assert k["cac_eur"] is None and k["roas"] is None  # no new paid customers, no spend in October
    assert metrics.pnl(*metrics.period("last_month", TODAY))["net_revenue"] == 200.0  # refund booked in Oct only


def test_empty_period_returns_zeros(two_months):
    start, end = metrics.period("today", date(2020, 1, 1))
    p = metrics.pnl(start, end)
    assert all(v == 0 for v in p.values())
    k = metrics.kpis(start, end)
    assert k["orders"] == k["customers"] == 0 and k["aov_eur"] == k["ltv_eur"] == k["repeat_rate_pct"] == 0
    assert k["cac_eur"] is None and k["roas"] is None and k["ai_cost_per_order_eur"] is None
