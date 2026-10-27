"""H8: EU digital content - immediate delivery only with the customer's express consent, recorded per order."""

from datetime import timedelta

from sqlalchemy import select

from aecom import reconciler
from aecom.bootstrap import seed_demo
from aecom.bus.events import drain
from aecom.chaos import invariants
from aecom.db import LedgerEntry, LicenseKey, Order, OutboxEmail, now, session
from aecom.monitors import undelivered_order
from aecom.store.adapter import get_store
from aecom.store.mock import WITHDRAWAL_CONSENT_VERSION

SKU = "LIC-FOCUSFLOW-PRO-V1"


def test_default_order_records_consent_with_version_and_delivers():
    seed_demo()
    order = get_store().create_order("a@example.com", SKU, "DE")
    with session() as s:
        o = s.get(Order, order["id"])
        assert o.withdrawal_consent_at == o.created_at
        assert o.withdrawal_consent_version == WITHDRAWAL_CONSENT_VERSION == "2026-10-v1"
    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"


def test_without_consent_order_is_paid_but_not_delivered():
    seed_demo()
    store = get_store()
    order = store.create_order("b@example.com", SKU, "DE", withdrawal_consent=False)
    with session() as s:
        o = s.get(Order, order["id"])
        assert o.withdrawal_consent_at is None and o.withdrawal_consent_version is None
        assert s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order["id"])).first() is not None
    drain()
    o = store.get_order(order["id"])
    assert o["status"] == "awaiting_consent" and not o["email_sent"]
    assert store.deliver_order(order["id"])["status"] == "awaiting_consent"  # a direct call is held too
    with session() as s:
        assert s.scalars(select(LicenseKey).where(LicenseKey.order_id == order["id"])).first() is None
        assert s.scalars(select(OutboxEmail).where(OutboxEmail.to == "b@example.com")).first() is None
    assert invariants.check_all() == []  # not "paid but undelivered", ledger balanced, no dead letters


def test_awaiting_consent_is_not_reconciled_or_flagged():
    seed_demo()
    order = get_store().create_order("c@example.com", SKU, "DE", withdrawal_consent=False)
    drain()
    with session() as s:
        s.get(Order, order["id"]).created_at = now() - timedelta(hours=2)
    assert reconciler.reconcile()["checked"] == 0
    assert undelivered_order(now()) == []


def test_later_consent_releases_delivery():
    seed_demo()
    store = get_store()
    order = store.create_order("d@example.com", SKU, "DE", withdrawal_consent=False)
    drain()
    after = store.record_withdrawal_consent(order["id"])
    assert after["status"] == "paid" and after["withdrawal_consent_version"] == WITHDRAWAL_CONSENT_VERSION
    assert store.deliver_order(order["id"])["status"] == "delivered"
