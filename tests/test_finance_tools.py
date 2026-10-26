"""Finance tools delegate to aecom.metrics; demo seeding copes with no licence key source."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from aecom import ledger, metrics
from aecom.config import get_settings
from aecom.db import LicenseKey, Product, now, session
from aecom.simulator import _backdate
from aecom.store.adapter import get_store
from aecom.tools.catalog import CATALOG

get_pnl, get_kpis = CATALOG["get_pnl"].fn, CATALOG["get_kpis"].fn


@pytest.fixture
def this_and_last_month():
    """One order on the 1st of the current month, one in the middle of the previous month."""
    store = get_store()
    store.create_product("DL-FIN-V1", "Finance test", 100.0, "download", unit_cost_eur=10.0)
    store.set_status("DL-FIN-V1", "active")
    month = datetime(now().year, now().month, 1)
    for email, ts in (("a@example.com", month), ("b@example.com", month - timedelta(days=15))):
        _backdate(store.create_order(email, "DL-FIN-V1", country="US")["id"], ts)


def test_all_time_matches_metrics_and_ledger(this_and_last_month):
    assert get_pnl() == metrics.pnl() == ledger.pnl()
    assert get_kpis() == metrics.kpis()
    assert get_kpis()["orders"] == 2


def test_period_filters(this_and_last_month):
    p = get_pnl("month_to_date")
    assert p == metrics.pnl(*metrics.period("month_to_date"))
    assert p["net_revenue"] == 100.0 and get_pnl()["net_revenue"] == 200.0
    k = get_kpis(period="month_to_date")
    assert k["orders"] == 1 and k["net_revenue_eur"] == 100.0
    assert get_kpis("last_month")["orders"] == 1


def test_unknown_period_lists_valid_names():
    for fn in (get_pnl, get_kpis):
        with pytest.raises(ValueError) as e:
            fn("forever")
        assert all(name in str(e.value) for name in metrics.PERIODS)
        assert all(name in fn.__doc__ for name in metrics.PERIODS)


def test_seed_demo_without_key_source_leaves_licence_draft(monkeypatch):
    from aecom.bootstrap import DEMO_PRODUCTS, seed_demo

    monkeypatch.setattr(get_settings(), "license_key_source", "none")
    seed_demo()
    store = get_store()
    for sku, _title, _kind, _price, _cost, _vendor, keys in DEMO_PRODUCTS:
        assert store.get_product(sku)["status"] == ("draft" if keys else "active")
    with session() as s:
        assert s.scalar(select(func.count(LicenseKey.id)).join(Product).where(Product.kind == "license")) == 0


def test_seed_demo_with_mock_source_stocks_and_activates_licence():
    from aecom.bootstrap import seed_demo

    seed_demo()
    assert get_store().get_product("LIC-FOCUSFLOW-PRO-V1")["status"] == "active"
    with session() as s:
        assert s.scalar(select(func.count(LicenseKey.id))) == 40
