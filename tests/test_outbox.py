"""I11: transactional outbox. Order (or refund), ledger lines and the bus event commit in one transaction."""

import pytest
from sqlalchemy import func, select

from aecom import ledger
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import publish
from aecom.db import Customer, EventRow, LedgerEntry, Order, Refund, session
from aecom.store import mock
from aecom.store.adapter import get_store

SKU = "LIC-FOCUSFLOW-PRO-V1"
EMAIL = "outbox@example.com"


class Crash(RuntimeError):
    pass


def crash(*_a, **_k):
    raise Crash("chaos: process died mid-transaction")


def fail_at(monkeypatch, step: str) -> None:
    """Crash after the order/refund rows are written: at the ledger post, or at the event publish after it."""
    monkeypatch.setattr(*((ledger, "post") if step == "ledger" else (mock, "publish")), crash)


def count(model, *where) -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where)) or 0


def snapshot() -> dict[str, int]:
    return {"orders": count(Order), "customers": count(Customer), "ledger": count(LedgerEntry),
            "events": count(EventRow), "refunds": count(Refund)}


@pytest.fixture(autouse=True)
def demo():
    seed_demo()


@pytest.mark.parametrize("step", ["ledger", "event"])
def test_crash_in_create_order_leaves_no_partial_rows(monkeypatch, step):
    before = snapshot()
    fail_at(monkeypatch, step)
    with pytest.raises(Crash):
        get_store().create_order(EMAIL, SKU, "DE")
    assert snapshot() == before
    assert count(Customer, Customer.email == EMAIL) == 0


def test_create_order_commits_order_ledger_and_event_together():
    order = get_store().create_order(EMAIL, SKU, "DE")
    assert count(LedgerEntry, LedgerEntry.ref == order["id"]) > 0
    with session() as s:
        paid = [e for e in s.scalars(select(EventRow).where(EventRow.topic == "order.paid"))
                if e.payload.get("order_id") == order["id"]]
    assert len(paid) == 1 and paid[0].source == "store"


@pytest.mark.parametrize("step", ["ledger", "event"])
def test_crash_in_refund_leaves_no_partial_rows(monkeypatch, step):
    store = get_store()
    order = store.create_order(EMAIL, SKU, "DE")
    store.deliver_order(order["id"])
    before = snapshot()
    fail_at(monkeypatch, step)
    with pytest.raises(Crash):
        store.refund_order(order["id"], 10.0, "crash test")
    assert snapshot() == before
    assert store.get_order(order["id"])["status"] == "delivered"


def test_refund_commits_refund_ledger_and_event_together():
    store = get_store()
    order = store.create_order(EMAIL, SKU, "DE")
    store.deliver_order(order["id"])
    store.refund_order(order["id"], 10.0, "partial")
    assert count(Refund, Refund.order_id == order["id"]) == 1
    assert count(LedgerEntry, LedgerEntry.ref == order["id"], LedgerEntry.memo.startswith("refund ")) > 0
    assert count(EventRow, EventRow.topic == "order.refunded") == 1


def test_publish_and_post_with_session_roll_back_with_it():
    before = snapshot()
    with pytest.raises(Crash):
        with session() as s:
            publish("test.outbox", {"x": 1}, session=s)
            ledger.post([("cash", 1.0, 0.0), ("revenue", 0.0, 1.0)], "outbox", session=s)
            raise Crash("rollback")
    assert snapshot() == before
    with session() as s:
        publish("test.outbox", {"x": 2}, session=s)
        ledger.post([("cash", 1.0, 0.0), ("revenue", 0.0, 1.0)], "outbox", session=s)
    assert count(EventRow, EventRow.topic == "test.outbox") == 1
    assert count(LedgerEntry, LedgerEntry.memo == "outbox") == 2


def test_outbox_context_scopes_publish_to_the_session():
    with pytest.raises(Crash):
        with session() as s, events.outbox(s):
            publish("test.outbox", {})
            raise Crash("rollback")
    assert count(EventRow, EventRow.topic == "test.outbox") == 0
    publish("test.outbox", {})  # outside the block: its own transaction again
    assert count(EventRow, EventRow.topic == "test.outbox") == 1
