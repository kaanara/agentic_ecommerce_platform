"""R10: AgentMail's send API has no idempotency key, so a crash between a successful send and `mark_email_sent`
could double-send a customer's licence key / download link. A durable 'send once' guard (MockStore.begin_email_send
/ mark_email_sent / fail_email_send) closes that gap: a crash leaves the marker 'sending' forever, and
`deliver_order` refuses to guess - it raises a P2 incident and waits for a human to clear it explicitly.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom import incidents
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain
from aecom.db import Order, OutboxEmail, session
from aecom.handlers import invoke_as
from aecom.integrations.agentmail import AgentMail
from aecom.store.adapter import EMAIL_SEND_SENDING, get_store
from aecom.store.mock import STUCK_EMAIL_MONITOR, MockStore

LICENSE_SKU = "LIC-FOCUSFLOW-PRO-V1"
DOWNLOAD_SKU = "TPL-NOTION-FREELANCE-V1"
EMAIL = "mail@example.com"


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    drain()  # settle whatever seeding queued


def emails_to(to: str) -> list[OutboxEmail]:
    with session() as s:
        return list(s.scalars(select(OutboxEmail).where(OutboxEmail.direction == "out", OutboxEmail.to == to)))


def order_row(order_id: str) -> tuple[str, bool]:
    with session() as s:
        o = s.get(Order, order_id)
        return o.status, o.email_sent


@pytest.fixture
def mark_sent_crashes_once(monkeypatch):
    """Simulate a crash between a successful AgentMail send and `mark_email_sent`: the send completes (one
    outbox row lands), then the process dies before the order/guard are updated to reflect it."""
    real, calls = MockStore.mark_email_sent, {"n": 0}

    def flaky(self, order_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("chaos: process died before mark_email_sent")
        return real(self, order_id)

    monkeypatch.setattr(MockStore, "mark_email_sent", flaky)
    return calls


@pytest.fixture
def send_fails_once(monkeypatch):
    """A definite failure: AgentMail.send itself raises, before anything reaches the customer."""
    real, calls = AgentMail.send, {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("chaos: AgentMail unavailable")
        return real(self, *a, **k)

    monkeypatch.setattr(AgentMail, "send", flaky)
    return calls


def test_normal_send_marks_sent_once():
    store = get_store()
    order = store.create_order(EMAIL, LICENSE_SKU, "DE")
    drain()

    status, sent = order_row(order["id"])
    assert status == "delivered" and sent is True
    assert len(emails_to(EMAIL)) == 1
    assert store.email_guard_state(order["id"]) is None  # the guard is cleared once confirmed sent

    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})  # a duplicate call sends nothing more
    assert len(emails_to(EMAIL)) == 1


def test_crash_between_send_and_mark_leaves_sending_and_second_attempt_does_not_resend(mark_sent_crashes_once):
    store = get_store()
    order = store.create_order(EMAIL, LICENSE_SKU, "DE")

    with pytest.raises(events.HandlerFailed):  # the gate reports ERROR: mark_email_sent "crashed"
        invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})

    # the AgentMail call itself succeeded before the "crash": the customer already has one email
    assert len(emails_to(EMAIL)) == 1
    status, sent = order_row(order["id"])
    assert status == "delivered" and sent is False
    assert store.email_guard_state(order["id"]) == EMAIL_SEND_SENDING

    # a second attempt (bus retry, reconciler, or a human calling deliver_order again) must not guess and resend
    result = invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    assert "email_delivery_blocked" in result or "blocked" in result.lower()
    assert len(emails_to(EMAIL)) == 1  # still exactly one
    assert store.email_guard_state(order["id"]) == EMAIL_SEND_SENDING  # still stuck, not auto-cleared

    incident = next(i for i in incidents.list_incidents() if i["monitor"] == STUCK_EMAIL_MONITOR)
    assert incident["severity"] == "P2" and incident["subject"] == order["id"]

    # a third attempt refreshes the same incident, not a new one
    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    stuck = [i for i in incidents.list_incidents() if i["monitor"] == STUCK_EMAIL_MONITOR]
    assert len(stuck) == 1 and len(emails_to(EMAIL)) == 1


def test_definite_failure_allows_retry(send_fails_once):
    store = get_store()
    order = store.create_order(EMAIL, LICENSE_SKU, "DE")

    with pytest.raises(events.HandlerFailed):
        invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    assert emails_to(EMAIL) == []  # the send itself raised: nothing reached the customer
    assert store.email_guard_state(order["id"]) is None  # cleared - free to retry
    status, sent = order_row(order["id"])
    assert status == "delivered" and sent is False

    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})  # retry succeeds
    assert len(emails_to(EMAIL)) == 1
    assert order_row(order["id"]) == ("delivered", True)
    assert send_fails_once["n"] == 2


def test_resend_after_a_cleared_stuck_guard_reuses_the_same_download_link(mark_sent_crashes_once):
    store = get_store()
    order = store.create_order(EMAIL, DOWNLOAD_SKU, "DE")

    with pytest.raises(events.HandlerFailed):
        invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    [first] = emails_to(EMAIL)
    assert store.email_guard_state(order["id"]) == EMAIL_SEND_SENDING

    # the human verifies (AgentMail sent folder / the customer) that a resend is needed, then clears the guard
    with pytest.raises(ValueError):
        store.clear_stuck_email_send(order["id"], by="")  # needs a name
    store.clear_stuck_email_send(order["id"], by="ops@example.com")
    assert store.email_guard_state(order["id"]) is None

    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})  # now allowed to send again
    sent = emails_to(EMAIL)
    assert len(sent) == 2
    link = next(w for w in first.body.split() if w.startswith("http"))
    assert link and all(link in e.body for e in sent)  # the same download link both times
    assert order_row(order["id"]) == ("delivered", True)


def test_partially_refunded_unemailed_order_gets_its_access_email_exactly_once_despite_a_crash(mark_sent_crashes_once):
    store = get_store()
    order = store.create_order(EMAIL, LICENSE_SKU, "DE")
    store.deliver_order(order["id"])
    store.refund_order(order["id"], 10.0, "partial")
    with session() as s:
        s.get(Order, order["id"]).email_sent = False  # the access email never went out

    with pytest.raises(events.HandlerFailed):  # crash between the send and mark_email_sent
        invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    assert len(emails_to(EMAIL)) == 1
    assert store.email_guard_state(order["id"]) == EMAIL_SEND_SENDING

    # further automatic attempts (bus retry, reconciler) must not send a second copy of the access
    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    invoke_as("store_ops", "deliver_order", {"order_id": order["id"]})
    assert len(emails_to(EMAIL)) == 1
    assert store.get_order(order["id"])["status"] == "partially_refunded"
    assert any(i["monitor"] == STUCK_EMAIL_MONITOR for i in incidents.list_incidents())
