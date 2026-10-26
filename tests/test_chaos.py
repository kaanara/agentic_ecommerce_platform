"""Chaos testing foundation (G1 invariants, G2 injectors + scenarios)."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from aecom import db, ledger
from aecom.bootstrap import init, seed_demo
from aecom.bus import events
from aecom.bus.events import drain, publish, subscribe
from aecom.chaos import injectors, invariants, run_scenario
from aecom.chaos.invariants import CHECKS, check_all
from aecom.db import AgentRun, Campaign, EventRow, LedgerEntry, LicenseKey, Order, Refund, session
from aecom.graph import agents
from aecom.store.adapter import get_store
from aecom.store.mock import MockStore

SKU = "LIC-FOCUSFLOW-PRO-V1"


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)


@pytest.fixture
def demo(no_backoff):
    """A clean demo company: products, a few delivered orders, drained bus."""
    seed_demo()
    orders = [get_store().create_order(f"c{i}@example.com", SKU, "DE")["id"] for i in range(3)]
    drain()
    return orders


def names(violations):
    return {v["invariant"] for v in violations}


# ----------------------------------------------------------------------------- invariants
def test_clean_demo_has_no_violations(demo):
    assert check_all() == []


def test_paid_but_undelivered_order_is_a_violation():
    seed_demo()
    oid = get_store().create_order("a@example.com", SKU, "DE")["id"]  # bus not drained
    assert [v["detail"] for v in invariants.every_paid_order_delivered()] == [f"order {oid} is still 'paid'"]


def test_unbalanced_ledger_is_a_violation(demo):
    with session() as s:
        s.add(LedgerEntry(txn_id="txn_bad", account="cash", debit=10.0, credit=0.0))
    assert len(invariants.ledger_balanced()) == 2  # the transaction and the total
    assert "txn_bad" in invariants.ledger_balanced()[0]["detail"]


def test_ledger_compares_integer_cents_not_floats(demo):
    ledger.post([("cash", 0.1 + 0.2, 0.0), ("revenue", 0.0, 0.3)], "float noise")
    assert invariants.ledger_balanced() == []


def test_order_with_two_keys_is_a_violation(demo):
    with session() as s:
        spare = s.scalars(select(LicenseKey).where(LicenseKey.status == "available")).first()
        spare.status, spare.order_id = "assigned", demo[0]
    assert names(check_all()) == {"no_key_on_two_orders"}


def test_refund_above_order_amount_is_a_violation(demo):
    with session() as s:
        s.add(Refund(order_id=demo[0], amount_eur=49.01, reason="test"))
    assert names(check_all()) == {"refunds_not_above_order_amount"}


def test_order_without_ledger_lines_is_a_violation(demo):
    with session() as s:
        o = s.get(Order, demo[0])
        s.add(Order(id="ord_unbooked", customer_id=o.customer_id, product_id=o.product_id, amount_eur=49.0,
                    status="delivered"))
    assert [v["detail"] for v in check_all()] == ["order ord_unbooked has no ledger lines"]


def test_marketing_overspend_is_a_violation(demo):
    from aecom.company_settings import set_setting

    set_setting("marketing_budget_month_eur", "100")
    with session() as s:
        s.add(Campaign(name="c", channel="google_ads", budget_eur=100, spent_eur=100.0, status="ended"))
    assert check_all() == []
    with session() as s:
        s.add(Campaign(name="d", channel="meta_ads", budget_eur=10, spent_eur=0.01, status="live"))
    assert names(check_all()) == {"marketing_spend_within_budget"}


def test_run_stuck_running_is_a_violation(demo):
    with session() as s:
        s.add(AgentRun(id="run_fresh", run_id="r1", agent_id="support", model="scripted", task="t", status="running"))
        s.add(AgentRun(id="run_old", run_id="r2", agent_id="support", model="scripted", task="t", status="running",
                       started_at=db.now() - timedelta(minutes=31)))
    assert [v["detail"].split()[1] for v in check_all()] == ["run_old"]  # a run that just started is fine


def test_dead_letter_is_a_violation(no_backoff, monkeypatch):
    @subscribe("test.chaos_dead")
    def always_fails(evt):
        raise RuntimeError("broken")

    monkeypatch.setattr(events, "MAX_ATTEMPTS", 1)
    publish("test.chaos_dead", {})
    drain()
    assert names(check_all()) == {"no_dead_letters"}


def test_every_check_runs_in_check_all():
    assert len(CHECKS) == 8 and all(fn() == [] for fn in CHECKS.values())


# ----------------------------------------------------------------------------- injectors
def test_delivery_failures_are_seeded_and_undone():
    real = MockStore.deliver_order

    def outcomes(seed):
        with injectors.delivery_failures(0.5, seed) as stats:
            for _ in range(20):
                try:
                    MockStore().deliver_order("missing")
                except RuntimeError:
                    pass
                except ValueError:  # the real method ran: order not found
                    pass
        return stats

    a, b = outcomes(7), outcomes(7)
    assert a == b and 0 < a["failures"] < 20
    assert MockStore.deliver_order is real


def test_duplicate_order_paid_publishes_twice(no_backoff):
    seed_demo()
    with injectors.duplicate_order_paid() as stats:
        get_store().create_order("dup@example.com", SKU, "DE")
    with session() as s:
        assert len(list(s.scalars(select(EventRow).where(EventRow.topic == "order.paid")))) == 2
    assert stats["duplicates"] == 1
    assert events.publish.__name__ == "publish"  # restored


def test_claude_down_makes_agent_runs_fail():
    import asyncio

    with injectors.claude_down():
        assert asyncio.run(agents.run_agent("support", "hello")).status == "error"
    assert asyncio.run(agents.run_agent("support", "hello")).status == "ok"


def test_db_locked_once_fails_only_the_first_call():
    with injectors.db_locked_once("aecom.ledger.post") as stats:
        with pytest.raises(OperationalError, match="database is locked"):
            ledger.post([("cash", 1.0, 0.0), ("revenue", 0.0, 1.0)], "first")
        ledger.post([("cash", 1.0, 0.0), ("revenue", 0.0, 1.0)], "second")
    assert stats == {"calls": 2, "failures": 1}
    with session() as s:
        assert [e.memo for e in s.scalars(select(LedgerEntry))] == ["second", "second"]


def test_db_locked_once_patches_methods():
    seed_demo()
    with injectors.db_locked_once("aecom.store.mock.MockStore.get_product"):
        with pytest.raises(OperationalError):
            get_store().get_product(SKU)
        assert get_store().get_product(SKU)["sku"] == SKU


# ----------------------------------------------------------------------------- scenarios
def test_delivery_crash_ends_without_violations(no_backoff):
    result = run_scenario("delivery-crash", seed=0, orders=20)
    assert result["stats"]["failures"] > 0  # faults really fired...
    assert result["violations"] == []       # ...and A1 retries delivered every order anyway
    with session() as s:
        assert {o.status for o in s.scalars(select(Order))} == {"delivered"}


def test_duplicate_paid_gives_no_key_to_two_orders(no_backoff):
    result = run_scenario("duplicate-paid", seed=1, orders=10)
    assert result["stats"]["duplicates"] == 10 and result["violations"] == []
    with session() as s:
        assigned = list(s.scalars(select(LicenseKey.order_id).where(LicenseKey.order_id.is_not(None))))
    assert len(assigned) == len(set(assigned)) > 0


def test_claude_down_email_succeeds_after_recovery(no_backoff):
    result = run_scenario("claude-down", seed=0, orders=2)
    assert result["violations"] == [] and result["stats"]["model_calls_failed"] > 0
    with session() as s:
        email = s.scalars(select(EventRow).where(EventRow.topic == "email.received")).one()
        statuses = {r.status for r in s.scalars(select(AgentRun).where(AgentRun.agent_id == "support"))}
    assert email.processed and not email.dead
    assert statuses == {"error", "ok"}


def test_unknown_scenario_is_rejected():
    with pytest.raises(ValueError, match="unknown chaos scenario"):
        run_scenario("nope")


def test_scenarios_are_deterministic_for_a_seed(no_backoff, tmp_path):
    def fresh_run(i):
        db.reset_engine(f"sqlite:///{tmp_path / f'chaos{i}.db'}")
        init()
        result = run_scenario("delivery-crash", seed=3, orders=15)
        with session() as s:
            skus = [o.product_id for o in s.scalars(select(Order).order_by(Order.created_at, Order.id))]
            countries = sorted(e.country for e in s.scalars(select(db.Customer)))
        return result, len(skus), countries

    assert fresh_run(1) == fresh_run(2)
