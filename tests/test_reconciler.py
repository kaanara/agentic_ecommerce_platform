"""Order reconciler (A2): re-publishes order.paid for paid orders whose event was lost or dead-lettered."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom import db
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain
from aecom.db import AuditLog, EventRow, Order, session
from aecom.reconciler import reconcile
from aecom.store.adapter import get_store

SKU = "LIC-FOCUSFLOW-PRO-V1"


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    drain()  # settle whatever seeding queued


def age(order_id, minutes=30):
    with session() as s:
        s.get(Order, order_id).created_at = db.now() - timedelta(minutes=minutes)


def lose_event(order_id, dead=False):
    """Simulate a lost delivery: the order.paid event is marked processed (or dead) without being delivered."""
    with session() as s:
        for e in s.scalars(select(EventRow).where(EventRow.topic == "order.paid")):
            if e.payload.get("order_id") == order_id:
                e.processed, e.dead = (False, True) if dead else (True, False)


def paid_events(order_id):
    with session() as s:
        return [e for e in s.scalars(select(EventRow).where(EventRow.topic == "order.paid"))
                if e.payload.get("order_id") == order_id]


@pytest.mark.parametrize("dead", [False, True])
def test_lost_event_is_requeued_and_delivered(dead):
    order = get_store().create_order("lost@example.com", SKU)
    lose_event(order["id"], dead=dead)
    age(order["id"])

    assert reconcile() == {"checked": 1, "requeued": 1, "already_pending": 0}
    requeued = paid_events(order["id"])[-1]
    assert requeued.source == "reconciler"
    assert requeued.payload == {"order_id": order["id"], "sku": SKU, "email": "lost@example.com", "reconciled": True}
    with session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "reconcile")).first().decision == "info"

    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"
    assert reconcile() == {"checked": 0, "requeued": 0, "already_pending": 0}  # delivered orders are untouched


def test_recent_order_is_skipped():
    order = get_store().create_order("new@example.com", SKU)
    lose_event(order["id"])
    assert reconcile() == {"checked": 0, "requeued": 0, "already_pending": 0}
    assert len(paid_events(order["id"])) == 1


def test_pending_event_is_not_duplicated_and_reconcile_is_idempotent():
    pending = get_store().create_order("pending@example.com", SKU)  # its order.paid is still queued
    lost = get_store().create_order("lost@example.com", SKU)
    lose_event(lost["id"])
    age(pending["id"]), age(lost["id"])

    assert reconcile() == {"checked": 2, "requeued": 1, "already_pending": 1}
    assert reconcile() == {"checked": 2, "requeued": 0, "already_pending": 2}  # the requeued event is pending
    assert len(paid_events(pending["id"])) == 1 and len(paid_events(lost["id"])) == 2

    drain()
    assert {get_store().get_order(o["id"])["status"] for o in (pending, lost)} == {"delivered"}


def test_failed_order_without_pending_restock_is_requeued():
    order = get_store().create_order("nokeys@example.com", SKU)
    lose_event(order["id"])
    with session() as s:
        s.get(Order, order["id"]).status = "failed"  # delivery hit "no keys"; its restock event was lost too
    age(order["id"])
    assert reconcile()["requeued"] == 1
    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"
