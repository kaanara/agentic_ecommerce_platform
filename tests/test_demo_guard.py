"""R17: tests for aecom.safety.assert_demo_data_allowed, the guard shared by simulator.simulate_day,
bootstrap.seed_demo and scenarios.run_scenario (mirrors chaos.run_scenario's existing _is_disposable_db check,
reusing the same definition rather than re-implementing it)."""

from __future__ import annotations

import tempfile

import pytest

from aecom import db
from aecom.bootstrap import init
from aecom.config import get_settings
from aecom.db import Customer, Order, Product, now, session
from aecom.safety import DemoDataRefused, assert_demo_data_allowed


def _non_disposable_url(tmp_path, monkeypatch, filename: str) -> str:
    """Point the system temp dir somewhere unrelated to tmp_path, so a real sqlite file under tmp_path (which we
    can freely create and query in a test) is no longer recognised as disposable by _is_disposable_db."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "unrelated-temp-root"))
    return f"sqlite:///{tmp_path / filename}"


def _use_db(url: str) -> None:
    get_settings().database_url = url
    db.reset_engine()
    init()


def _record_real_sale() -> None:
    """Insert one REAL order directly: external_id set (came from a real store webhook) and paid."""
    with session() as s:
        c = Customer(email="real-customer@example.com", country="DE")
        s.add(c)
        s.flush()
        p = Product(sku="REAL-SKU-1", title="Real product", kind="download", price_eur=10.0, status="active")
        s.add(p)
        s.flush()
        s.add(Order(customer_id=c.id, product_id=p.id, amount_eur=10.0, vat_eur=0.0, status="paid",
                    external_id="real-store-order-1", created_at=now()))


def test_refused_with_real_sales_on_a_non_disposable_database(tmp_path, monkeypatch):
    _use_db(_non_disposable_url(tmp_path, monkeypatch, "company.db"))
    _record_real_sale()
    with pytest.raises(DemoDataRefused, match="seed_demo"):
        assert_demo_data_allowed("bootstrap.seed_demo")


def test_allowed_on_a_disposable_tmp_database():
    # the autouse fresh_company fixture already points DATABASE_URL at a file under the system temp dir
    assert_demo_data_allowed("simulator.simulate_day")  # must not raise


def test_allowed_on_a_fresh_non_disposable_database_without_real_sales(tmp_path, monkeypatch):
    """`aecom init --demo` must keep working the first time it points at a brand-new company database."""
    _use_db(_non_disposable_url(tmp_path, monkeypatch, "fresh-company.db"))
    assert_demo_data_allowed("bootstrap.seed_demo")  # no real sales yet: allowed


def test_env_override_allows_even_with_real_sales(tmp_path, monkeypatch):
    _use_db(_non_disposable_url(tmp_path, monkeypatch, "company2.db"))
    _record_real_sale()
    monkeypatch.setenv("AECOM_ALLOW_DEMO", "1")
    assert_demo_data_allowed("bootstrap.seed_demo")  # explicit operator override


def test_error_message_names_the_action(tmp_path, monkeypatch):
    _use_db(_non_disposable_url(tmp_path, monkeypatch, "company3.db"))
    _record_real_sale()
    with pytest.raises(DemoDataRefused, match="simulator.simulate_day"):
        assert_demo_data_allowed("simulator.simulate_day")


def test_simulate_day_and_seed_demo_are_refused_on_a_non_disposable_db_with_real_sales(tmp_path, monkeypatch):
    from aecom.bootstrap import seed_demo
    from aecom.simulator import simulate_day

    _use_db(_non_disposable_url(tmp_path, monkeypatch, "company4.db"))
    _record_real_sale()
    with pytest.raises(DemoDataRefused):
        seed_demo()
    with pytest.raises(DemoDataRefused):
        simulate_day()


def test_scenarios_run_scenario_is_refused_on_a_non_disposable_db_with_real_sales(tmp_path, monkeypatch):
    import asyncio

    from aecom.scenarios import run_scenario

    _use_db(_non_disposable_url(tmp_path, monkeypatch, "company5.db"))
    _record_real_sale()
    with pytest.raises(DemoDataRefused):
        asyncio.run(run_scenario("bootstrap", offline=True))
