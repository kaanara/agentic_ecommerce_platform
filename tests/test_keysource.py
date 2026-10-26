"""I5: restocks only use a configured licence key source; without one they fail loudly, once."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom.bus import events
from aecom.bus.events import drain, publish
from aecom.db import AuditLog, EventRow, session
from aecom.store.adapter import get_store
from aecom.store.keysource import MockKeySource, get_key_source
from aecom.tools.catalog import CATALOG

SKU = "LIC-KS-V1"


@pytest.fixture
def product():
    get_store().create_product(SKU, "Keysource test", 10.0, "license")
    return SKU


@pytest.fixture
def no_source(fresh_company, monkeypatch):
    monkeypatch.setattr(fresh_company, "license_key_source", "none")
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)


def available(sku: str) -> int:
    return next(r["available_keys"] for r in get_store().inventory() if r["sku"] == sku)


def test_mock_source_restocks_as_before(product):
    assert isinstance(get_key_source(), MockKeySource)
    assert CATALOG["add_license_keys"].fn(sku=product, count=3)["added"] == 3
    publish("inventory.low", {"sku": product, "available_keys": 3})
    drain()
    assert available(product) == 53


def test_no_source_refuses_to_mint_but_accepts_explicit_keys(product, no_source):
    assert get_key_source() is None
    with pytest.raises(ValueError, match="no licence key source configured"):
        CATALOG["add_license_keys"].fn(sku=product, count=5)
    assert CATALOG["add_license_keys"].fn(sku=product, keys=["VENDOR-KEY-1", "VENDOR-KEY-2"])["added"] == 2
    assert available(product) == 2


@pytest.mark.parametrize("topic", ["inventory.low", "inventory.out_of_stock"])
def test_no_source_reflex_reports_restock_failed_once(product, no_source, topic):
    eid = publish(topic, {"sku": product, "available_keys": 0, "order_id": "ORD-NOPE"})
    drain()
    with session() as s:
        evt = s.get(EventRow, eid)
        assert (evt.processed, evt.attempts, evt.dead) == (True, 1, False)  # no HandlerFailed retry storm
        failed = s.scalars(select(EventRow).where(EventRow.topic == "inventory.restock_failed")).all()
        assert [e.payload["sku"] for e in failed] == [product]
        assert "no licence key source" in failed[0].payload["reason"]
        assert s.scalars(select(AuditLog).where(AuditLog.action == "restock_failed")).one().decision == "error"
        assert not s.scalars(select(AuditLog).where(AuditLog.action == f"event:{topic}")).all()
    assert available(product) == 0
