"""At-least-once event delivery (A1) and closing crashed agent runs (A3)."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import inspect, select, text

from aecom import db
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import dead_letters, drain, publish, retry, subscribe
from aecom.company_settings import set_setting
from aecom.db import AgentRun, AuditLog, EventRow, session
from aecom.store.adapter import get_store


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)


def event(event_id):
    with session() as s:
        return s.get(EventRow, event_id)


def test_failed_handler_is_retried_and_successful_handlers_are_not_repeated(monkeypatch):
    calls = {"ok": 0, "flaky": 0}

    @subscribe("test.retry")
    def ok_handler(evt):
        calls["ok"] += 1

    @subscribe("test.retry")
    def flaky_handler(evt):
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("temporary outage")

    eid = publish("test.retry", {})
    drain()
    e = event(eid)
    assert not e.processed and e.attempts == 1 and "temporary outage" in e.last_error
    assert e.done_handlers == ["ok_handler"] and e.next_attempt_at is not None

    drain()  # backoff not over yet → nothing happens
    assert calls == {"ok": 1, "flaky": 1}

    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    with session() as s:
        s.get(EventRow, eid).next_attempt_at = db.now() - timedelta(seconds=1)
    drain()
    e = event(eid)
    assert e.processed and e.last_error is None
    assert calls == {"ok": 1, "flaky": 2}  # the handler that had succeeded was not run again


def test_event_is_dead_lettered_after_max_attempts_and_can_be_retried(no_backoff):
    @subscribe("test.dead")
    def always_fails(evt):
        raise RuntimeError("broken")

    eid = publish("test.dead", {"x": 1})
    drain()
    e = event(eid)
    assert e.dead and not e.processed and e.attempts == events.MAX_ATTEMPTS
    assert [d["id"] for d in dead_letters()] == [eid]
    with session() as s:
        audits = list(s.scalars(select(AuditLog).where(AuditLog.action == "event:test.dead")))
    assert audits[-1].detail["dead_letter"] is True

    assert retry([eid]) == 1
    e = event(eid)
    assert not e.dead and e.attempts == 0


def test_claimed_event_waits_for_its_lease_before_being_retried():
    @subscribe("test.lease")
    def never_runs(evt):
        raise AssertionError("must not run while the lease is held")

    eid = publish("test.lease", {})
    with session() as s:  # as if another drain claimed it and then crashed
        e = s.get(EventRow, eid)
        e.attempts, e.next_attempt_at = 1, db.now() + events.LEASE
    assert drain() == 0


def test_delivery_blocked_by_kill_switch_is_retried_instead_of_lost(no_backoff, monkeypatch):
    seed_demo()
    set_setting("kill_switch", "on")
    order = get_store().create_order("buyer@example.com", "LIC-FOCUSFLOW-PRO-V1", "DE")
    monkeypatch.setattr(events, "MAX_ATTEMPTS", 1)  # keep the test quick: one try, then dead letter
    drain()
    assert get_store().get_order(order["id"])["status"] == "paid"  # not delivered, but not forgotten
    assert [d["topic"] for d in dead_letters()] == ["order.paid"]

    set_setting("kill_switch", "off")
    retry()
    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"


def test_crashed_agent_run_is_marked_error_not_running(monkeypatch):
    from aecom.graph import agents

    def boom(*a, **k):
        raise RuntimeError("ANTHROPIC_API_KEY missing")

    monkeypatch.setattr(agents, "get_model", boom)
    result = asyncio.run(agents.run_agent("support", "hello"))
    assert result.status == "error"
    with session() as s:
        run = s.scalars(select(AgentRun).where(AgentRun.agent_id == "support")).one()
    assert run.status == "error" and run.ended_at is not None and "ANTHROPIC_API_KEY" in run.output


def test_failed_customer_email_is_retried_when_support_recovers(no_backoff, monkeypatch):
    from aecom.graph import agents
    from aecom.llm import Script
    from aecom.scenarios import final

    real_get_model = agents.get_model
    monkeypatch.setattr(agents, "get_model", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("API down")))
    monkeypatch.setattr(events, "MAX_ATTEMPTS", 1)
    eid = publish("email.received", {"from": "maria.k@example.com", "subject": "Hi", "body": "Hello", "message_id": "m9"})
    drain()
    assert event(eid).dead

    monkeypatch.setattr(agents, "get_model", real_get_model)
    Script.load({"support": [final("Answered.")]})
    retry()
    drain()
    assert event(eid).processed
    with session() as s:
        statuses = [r.status for r in s.scalars(select(AgentRun).where(AgentRun.agent_id == "support"))]
    assert sorted(statuses) == ["error", "ok"]


def test_init_adds_new_columns_to_an_old_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    db.reset_engine(url)
    with db.get_engine().begin() as conn:  # the ops_events table as created by the previous version
        conn.execute(text("CREATE TABLE ops_events (id INTEGER PRIMARY KEY, ts DATETIME, topic VARCHAR, "
                          "payload JSON, source VARCHAR, processed BOOLEAN)"))
        conn.execute(text("INSERT INTO ops_events (topic, payload, source, processed) VALUES ('x', '{}', 's', 0)"))
    db.init_db()
    cols = {c["name"] for c in inspect(db.get_engine()).get_columns("ops_events")}
    assert {"attempts", "next_attempt_at", "done_handlers", "last_error", "dead"} <= cols
    with session() as s:
        old = s.scalars(select(EventRow)).one()
    assert old.attempts == 0 and old.dead is False


def test_new_email_sent_column_is_backfilled_for_delivered_orders(tmp_path):
    url = f"sqlite:///{tmp_path / 'old_orders.db'}"
    db.reset_engine(url)
    with db.get_engine().begin() as conn:  # commerce_orders as created before email tracking (I4)
        conn.execute(text("CREATE TABLE commerce_orders (id VARCHAR PRIMARY KEY, customer_id VARCHAR, product_id VARCHAR, "
                          "quantity INTEGER, amount_eur FLOAT, vat_eur FLOAT, campaign_id VARCHAR, status VARCHAR, created_at DATETIME)"))
        conn.execute(text("INSERT INTO commerce_orders (id, status, amount_eur) VALUES ('o1', 'delivered', 1), ('o2', 'paid', 1)"))
    db.init_db()
    with db.get_engine().begin() as conn:
        rows = dict(conn.execute(text("SELECT id, email_sent FROM commerce_orders")).all())
    assert rows == {"o1": 1, "o2": 0}
