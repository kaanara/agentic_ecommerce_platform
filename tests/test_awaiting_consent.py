"""R13b: orders held for withdrawal consent raise a P3 monitor after 24 h, and are not 'undelivered'."""

from datetime import timedelta

import pytest
from aecom import db, incidents, monitors
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain
from aecom.db import Order, session
from aecom.reconciler import reconcile
from aecom.store.adapter import get_store

SKU = "LIC-FOCUSFLOW-PRO-V1"


@pytest.fixture
def held(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    drain()
    store = get_store()
    oid = store.create_order("c@example.com", SKU, "DE", withdrawal_consent=False)["id"]
    drain()
    assert store.get_order(oid)["status"] == "awaiting_consent"
    return oid


def active():
    return {i["subject"]: i for i in incidents.list_incidents()
            if i["monitor"] == "awaiting_consent_order" and i["status"] in incidents.ACTIVE}


def test_registered_as_p3():
    assert monitors.MONITORS["awaiting_consent_order"][0] == "P3"


def test_raises_after_24h_not_before_and_resolves(held):
    t0 = db.now()
    monitors.run_monitors(now=t0 + timedelta(hours=23, minutes=50))
    assert active() == {}
    monitors.run_monitors(now=t0 + timedelta(hours=24, minutes=5))
    inc = active()[held]
    assert inc["severity"] == "P3" and "consent" in inc["detail"]

    get_store().record_withdrawal_consent(held)
    monitors.run_monitors(now=t0 + timedelta(hours=25))
    assert active() == {}


def test_not_treated_as_undelivered(held):
    with session() as s:
        s.get(Order, held).created_at = db.now() - timedelta(days=3)
    assert monitors.undelivered_order(db.now()) == []
    assert monitors.unemailed_order(db.now()) == []
    assert reconcile().get("requeued", 0) == 0
