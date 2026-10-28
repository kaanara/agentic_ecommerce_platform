"""Migration-trigger reminder: one P3 incident when cumulative net revenue reaches MIGRATION_REVENUE_EUR."""

from datetime import timedelta

import pytest
from aecom import alerts, incidents, ledger, monitors
from aecom.db import Order, session
from aecom.db import now as db_now
from aecom.secrets import secrets

NAME, SUBJECT = "revenue_milestone", "hosting_accounting_review"


@pytest.fixture(autouse=True)
def config(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get", lambda name: values.get(name))
    return values


@pytest.fixture
def routed(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(alerts, "route", lambda inc: sent.append(inc))
    monkeypatch.setattr(monitors, "AUTO_DIAGNOSE", False)
    return sent


def sale(order_id, net, real=True):
    """Book a sale; a real store order carries a platform id (external_id), a demo/simulated one does not."""
    with session() as s:
        s.add(Order(id=order_id, customer_id="cus_t", product_id="prd_t", amount_eur=net, vat_eur=0.0, status="delivered",
                    email_sent=True, external_id=f"ls-{order_id}" if real else None))
    ledger.record_sale(order_id, net, 0, 0)


def mine():
    return [i for i in incidents.list_incidents() if i["monitor"] == NAME]


def run(n=1, step=timedelta(minutes=5)):
    t = db_now()
    for k in range(n):
        monitors.run_monitors(t + step * k)


def test_registered_as_p3():
    assert monitors.MONITORS[NAME][0] == "P3"


def test_below_threshold_nothing(routed):
    sale("o1", 24.99)
    run(3)
    assert mine() == [] and routed == []


def test_crossing_raises_exactly_one_p3_and_never_duplicates(routed):
    sale("o1", 25.0)
    run(4)
    [inc] = mine()
    assert inc["severity"] == "P3" and inc["status"] == "open" and inc["subject"] == SUBJECT
    assert "Net revenue reached EUR 25.00 (threshold EUR 25.00)" in inc["detail"]
    assert "lexoffice or sevDesk" in inc["detail"] and "Moving to the cloud" in inc["detail"]
    assert len([r for r in routed if r["monitor"] == NAME]) == 1  # alerted once, no nagging


def test_acknowledged_stays_and_is_not_realerted(routed):
    sale("o1", 30)
    run()
    incidents.acknowledge(mine()[0]["id"], "ceo")
    n = len(routed)
    run(5, step=timedelta(hours=3))
    assert [i["status"] for i in mine()] == ["acknowledged"] and len(routed) == n


def test_never_returns_after_human_resolve(routed):
    sale("o1", 30)
    run()
    incidents.resolve(mine()[0]["id"], "ceo", "decided")
    sale("o2", 100)
    run(5, step=timedelta(hours=1))
    assert [i["status"] for i in mine()] == ["resolved"]
    assert len([r for r in routed if r["monitor"] == NAME]) == 1


def test_resolved_after_acknowledge_also_final(routed):
    sale("o1", 30)
    run()
    iid = mine()[0]["id"]
    incidents.acknowledge(iid, "ceo")
    incidents.resolve(iid, "ceo")
    run(3)
    assert len(mine()) == 1


def test_refunds_below_threshold_do_not_auto_resolve(routed):
    sale("o1", 30)
    run()
    ledger.record_refund("o1", 20)  # net revenue now 10, below 25
    run(3)
    [inc] = mine()
    assert inc["status"] == "open"
    assert "EUR 10.00" in inc["detail"]


def test_refund_before_first_run_stays_silent(routed):
    sale("o1", 30)
    ledger.record_refund("o1", 10)
    run()
    assert mine() == []


def test_custom_threshold(config, routed):
    config["MIGRATION_REVENUE_EUR"] = "10"
    sale("o1", 12)
    run()
    [inc] = mine()
    assert "threshold EUR 10.00" in inc["detail"]


def test_custom_threshold_not_reached(config, routed):
    config["MIGRATION_REVENUE_EUR"] = "100"
    sale("o1", 30)
    run()
    assert mine() == []


def test_invalid_threshold_falls_back_to_default(config, routed):
    config["MIGRATION_REVENUE_EUR"] = "lots"
    sale("o1", 30)
    run()
    assert len(mine()) == 1 and not [i for i in incidents.list_incidents() if i["monitor"] == "monitor_error"]


def test_other_monitors_unaffected(routed):
    sale("o1", 30)
    summary = monitors.evaluate()
    assert summary["monitor_errors"] == [] and summary["failing"] == {NAME: 1}
    assert {i["monitor"] for i in incidents.list_incidents()} == {NAME}


def test_demo_and_simulated_revenue_never_triggers_it(routed):
    """The seeded demo data (hundreds of euros, no store-platform ids) must not fire the reminder on day one."""
    sale("demo1", 500.0, real=False)
    sale("demo2", 900.0, real=False)
    run(3)
    assert mine() == [] and routed == []
    assert monitors.real_net_revenue_eur() == 0.0
    sale("o_real", 25.0)  # the first REAL sale at the threshold does
    run(2)
    assert len(mine()) == 1


def test_refunds_of_real_orders_reduce_the_counted_revenue():
    sale("o1", 40.0)
    assert monitors.real_net_revenue_eur() == 40.0
    ledger.record_refund("o1", 15.0)
    assert monitors.real_net_revenue_eur() == pytest.approx(25.0, abs=0.01)
