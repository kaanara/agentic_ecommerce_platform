"""I4/R6: the access email is tracked apart from delivery; refunded orders never get a new key."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom import db
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain
from aecom.db import EventRow, LicenseKey, OutboxEmail, Order, session
from aecom.handlers import invoke_as
from aecom.integrations.agentmail import AgentMail
from aecom.reconciler import reconcile
from aecom.store.adapter import get_store

SKU = "LIC-FOCUSFLOW-PRO-V1"
EMAIL = "mail@example.com"


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    drain()  # settle whatever seeding queued


def emails_to(to: str) -> list[OutboxEmail]:
    with session() as s:
        return list(s.scalars(select(OutboxEmail).where(OutboxEmail.direction == "out", OutboxEmail.to == to)))


def order_row(order_id: str) -> tuple[str, bool, object]:
    with session() as s:
        o = s.get(Order, order_id)
        return o.status, o.email_sent, o.email_sent_at


def keys_of(order_id: str) -> list[str]:
    with session() as s:
        return list(s.scalars(select(LicenseKey.key).where(LicenseKey.order_id == order_id)))


@pytest.fixture
def mail_fails_once(monkeypatch):
    real, calls = AgentMail.send, {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("chaos: AgentMail unavailable")
        return real(self, *a, **k)

    monkeypatch.setattr(AgentMail, "send", flaky)
    return calls


def test_failed_email_is_resent_by_the_bus_retry_exactly_once(mail_fails_once):
    order = get_store().create_order(EMAIL, SKU, "DE")
    drain(limit=1)  # delivery commits, the email send raises -> the event waits for a retry
    status, sent, sent_at = order_row(order["id"])
    assert (status, sent, sent_at) == ("delivered", False, None) and emails_to(EMAIL) == []
    key = keys_of(order["id"])
    assert len(key) == 1

    drain()  # zero backoff: the retry sees 'already delivered' and still sends the key
    status, sent, sent_at = order_row(order["id"])
    assert status == "delivered" and sent is True and sent_at is not None
    [mail] = emails_to(EMAIL)
    assert key[0] in mail.body and keys_of(order["id"]) == key
    with session() as s:
        assert all(e.processed for e in s.scalars(select(EventRow).where(EventRow.topic == "order.paid")))

    drain()
    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})  # a duplicate delivery sends nothing more
    assert len(emails_to(EMAIL)) == 1 and mail_fails_once["n"] == 2


def test_send_failure_surfaces_as_handler_failure(mail_fails_once):
    order = get_store().create_order(EMAIL, SKU, "DE")
    with pytest.raises(events.HandlerFailed, match="ERROR"):
        invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    assert order_row(order["id"])[:2] == ("delivered", False)


def test_reconciler_requeues_delivered_but_unemailed_orders(mail_fails_once):
    order = get_store().create_order(EMAIL, SKU, "DE")
    with session() as s:  # the order.paid event got lost after the failed send
        for e in s.scalars(select(EventRow).where(EventRow.topic == "order.paid")):
            e.processed = True
    with pytest.raises(events.HandlerFailed):
        invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    assert reconcile()["checked"] == 0  # too recent
    with session() as s:
        s.get(Order, order["id"]).created_at = db.now() - timedelta(minutes=30)

    assert reconcile() == {"checked": 1, "requeued": 0, "already_pending": 0, "requeued_email": 1}
    assert reconcile() == {"checked": 1, "requeued": 0, "already_pending": 1}  # requeued event still pending
    drain()
    assert order_row(order["id"])[:2] == ("delivered", True) and len(emails_to(EMAIL)) == 1
    assert reconcile() == {"checked": 0, "requeued": 0, "already_pending": 0}


def test_partially_refunded_order_keeps_its_single_key():
    store = get_store()
    order = store.create_order(EMAIL, SKU, "DE")
    drain()
    key = keys_of(order["id"])
    store.refund_order(order["id"], 10.0, "partial")

    result = store.deliver_order(order["id"])
    assert result["status"] == "partially_refunded" and result["already"]
    assert store.get_order(order["id"])["status"] == "partially_refunded"
    assert keys_of(order["id"]) == key and len(key) == 1
    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    assert len(emails_to(EMAIL)) == 1


def test_refunded_order_gets_no_new_key():
    store = get_store()
    order = store.create_order(EMAIL, SKU, "DE")
    drain()
    store.refund_order(order["id"], order["amount_eur"], "full")

    assert store.deliver_order(order["id"])["status"] == "refunded"
    assert store.get_order(order["id"])["status"] == "refunded"
    with session() as s:
        assert [k.status for k in s.scalars(select(LicenseKey).where(LicenseKey.order_id == order["id"]))] == ["revoked"]
