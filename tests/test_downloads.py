"""I13: one stable, expiring download link per order in the built-in store."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom.bootstrap import seed_demo
from aecom.db import AuditLog, LicenseKey, Order, Product, session
from aecom.db import now as real_now
from aecom.store import mock
from aecom.store.adapter import get_store
from aecom.store.mock import MockStore

DOWNLOAD = "TPL-NOTION-FREELANCE-V1"
LICENCE = "LIC-FOCUSFLOW-PRO-V1"


@pytest.fixture(autouse=True)
def demo():
    seed_demo()


@pytest.fixture
def store():
    return MockStore()


def order_row(order_id: str) -> Order:
    with session() as s:
        o = s.get(Order, order_id)
        s.expunge(o)
        return o


def delivered(store: MockStore, sku: str = DOWNLOAD) -> tuple[str, dict]:
    order = store.create_order("dl@example.com", sku, "DE")
    return order["id"], store.deliver_order(order["id"])


def token_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


def test_first_delivery_stores_token_and_expiry(store):
    order_id, result = delivered(store)
    o = order_row(order_id)
    assert o.download_token and len(o.download_token) >= 32
    assert result["download_url"] == f"https://downloads.example/{order_id}/{o.download_token}"
    assert timedelta(days=6, hours=23) < o.download_expires_at - real_now() <= timedelta(days=7)


def test_link_uses_config_and_never_exposes_asset_url(store, fresh_company, monkeypatch):
    with session() as s:
        s.scalars(select(Product).where(Product.sku == DOWNLOAD)).one().asset_url = "s3://private/notion.zip"
    monkeypatch.setattr(fresh_company, "download_base_url", "https://files.shop.test/")
    monkeypatch.setattr(fresh_company, "download_link_days", 2)
    order_id, result = delivered(store)
    o = order_row(order_id)
    assert result["download_url"] == f"https://files.shop.test/{order_id}/{o.download_token}"
    assert "s3://" not in result["download_url"] and "notion.zip" not in result["download_url"]
    assert o.download_expires_at - real_now() <= timedelta(days=2)


def test_resend_returns_the_identical_url(store):
    order_id, first = delivered(store)
    again = store.deliver_order(order_id)
    assert again["already"] and again["download_url"] == first["download_url"]
    assert "_link_reissued" not in again


def test_expired_link_is_reissued_and_audited_without_the_token(store, monkeypatch):
    order_id, first = delivered(store)
    old = order_row(order_id)
    monkeypatch.setattr(mock, "now", lambda: old.download_expires_at + timedelta(seconds=1))
    again = store.deliver_order(order_id)
    new = order_row(order_id)
    assert again["download_url"] != first["download_url"]
    assert new.download_token != old.download_token and token_of(again["download_url"]) == new.download_token
    assert new.download_expires_at == old.download_expires_at + timedelta(seconds=1, days=7)
    with session() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "download_link_reissued")))
    assert len(rows) == 1 and rows[0].detail["order_id"] == order_id
    assert old.download_token not in str(rows[0].detail) and new.download_token not in str(rows[0].detail)
    # the reissued link is stable again
    assert store.deliver_order(order_id)["download_url"] == again["download_url"]


def test_verify_download(store):
    order_id, result = delivered(store)
    token = token_of(result["download_url"])
    expires = order_row(order_id).download_expires_at
    assert store.verify_download(order_id, token)
    assert not store.verify_download(order_id, token[:-1] + ("A" if token[-1] != "A" else "B"))
    assert not store.verify_download(order_id, "")
    assert not store.verify_download("ord_missing", token)
    assert store.verify_download(order_id, token, now=expires - timedelta(seconds=1))
    assert not store.verify_download(order_id, token, now=expires)
    other_id, _ = delivered(store)
    assert not store.verify_download(other_id, token)  # a token only opens its own order


def test_verify_download_after_refunds(store):
    order_id, result = delivered(store)
    token = token_of(result["download_url"])
    store.refund_order(order_id, 10.0, "partial goodwill")
    assert store.verify_download(order_id, token)  # partially refunded: still allowed
    store.refund_order(order_id, 19.0, "rest")
    assert not store.verify_download(order_id, token)  # fully refunded: access ends


def test_undelivered_order_has_no_link(store):
    order = store.create_order("dl@example.com", DOWNLOAD, "DE", withdrawal_consent=False)
    assert store.deliver_order(order["id"])["status"] == "awaiting_consent"
    assert order_row(order["id"]).download_token is None


def test_licence_products_are_unaffected(store):
    order_id, result = delivered(store, LICENCE)
    assert result["license_key"] and "download_url" not in result
    assert "download_url" not in store.deliver_order(order_id)
    o = order_row(order_id)
    assert o.download_token is None and o.download_expires_at is None


def test_agent_tool_result_never_contains_the_key_or_link():
    from aecom.tools.catalog import CATALOG

    seed_demo()
    store = get_store()
    lic = store.create_order("k@example.com", "LIC-FOCUSFLOW-PRO-V1", "DE")["id"]
    dl = store.create_order("d@example.com", "TPL-NOTION-FREELANCE-V1", "DE")["id"]
    shown = {oid: str(CATALOG["deliver_order"].fn(order_id=oid)) for oid in (lic, dl)}  # delivered by the tool
    with session() as s:
        key = s.scalars(select(LicenseKey.key).where(LicenseKey.order_id == lic)).one()
        token = s.get(Order, dl).download_token
    assert key and key not in shown[lic] and key[-4:] in shown[lic]  # the agent sees only the last 4 chars
    assert token and token not in shown[dl]
