"""F3: Etsy as a second sales channel (`src/aecom/store/channels.py`, `src/aecom/store/etsy.py`). Offline only:
documented payload shapes from tests/fixtures/etsy (see its README) plus fakes for `net_guard`'s pinned HTTP and
`oauth_tokens`'s token refresh - no test here ever opens a socket.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select

from aecom import oauth_tokens
from aecom.bus.events import drain
from aecom.chaos.invariants import check_all, no_key_on_two_orders
from aecom.company_settings import get_setting
from aecom.db import LicenseKey, Order, OutboxEmail, Product, Refund, session
from aecom.oauth_tokens import OAuthError
from aecom.store import channels
from aecom.store import etsy
from aecom.store.adapter import get_store

FIX = Path(__file__).parent / "fixtures" / "etsy"
SHOP_ID = "555666"
CLIENT_ID = "test-etsy-client-id"
ACCESS_TOKEN = "etsy-access-token-1"
SINCE = datetime(2026, 10, 1)


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _response(method: str, target: str, status: int = 200, json: dict | None = None) -> httpx.Response:
    """A fake `httpx.Response` with ``.request`` set, so `raise_for_status()` works like it would for a real
    response (same thing `test_oauth_tokens.py`'s `FakeHTTP` does for `oauth_tokens`'s own fake)."""
    resp = httpx.Response(status, json=json)
    resp.request = httpx.Request(method, target)
    return resp


@pytest.fixture(autouse=True)
def _isolated_oauth_token_file(tmp_path, monkeypatch):
    """Every test gets its own OAuth token file, never the real `data/oauth_tokens.json` (same isolation
    `test_oauth_tokens.py` applies) - `_connect_oauth` below persists a connected record through the real
    `oauth_tokens` module, which would otherwise write real-looking credentials outside the per-test sandbox."""
    monkeypatch.setenv("OAUTH_TOKEN_FILE", str(tmp_path / "oauth_tokens.json"))


def _set_settings(settings, **kwargs) -> None:
    """Inject ``etsy_enabled``/``etsy_shop_id`` onto the live `Settings` singleton. They are not yet real
    `config.Settings` fields (that is a patch for the lead - see the final report / docs/wiki/Integrations.md),
    so `monkeypatch.setattr` can't be used (pydantic rejects assigning a field it doesn't declare); this writes
    straight into the instance's ``__dict__`` the same way pydantic itself stores field values, which is exactly
    what `getattr(get_settings(), 'etsy_enabled', False)` (the documented, literal read this task calls for) then
    finds. `fresh_company` (tests/conftest.py) already hands back a process-wide singleton per test *re-created*
    via `get_settings.cache_clear()` is never called between tests, so this is reverted explicitly, not relied on
    to vanish on its own."""
    for key, value in kwargs.items():
        object.__setattr__(settings, key, value)


def _connect_oauth(monkeypatch) -> None:
    """Connect the etsy OAuth provider and fake its token-refresh HTTP call (aecom.oauth_tokens), independent of
    the Etsy API client's own net_guard calls faked by `api()` below."""
    monkeypatch.setenv("ETSY_CLIENT_ID", CLIENT_ID)
    oauth_tokens.set_refresh_token("etsy", "initial-etsy-refresh-token", "tester")
    monkeypatch.setattr(oauth_tokens, "resolve_public", lambda url: url)  # identity: target is the bare url

    def fake_oauth_post(target, *, data=None, json=None, headers=None, timeout=15):
        return _response("POST", target, json={"access_token": ACCESS_TOKEN, "expires_in": 3600,
                                                "refresh_token": "initial-etsy-refresh-token"})

    monkeypatch.setattr(oauth_tokens, "pinned_post", fake_oauth_post)


@pytest.fixture(autouse=True)
def _clear_injected_etsy_settings(fresh_company):
    """`_set_settings` writes straight into the `Settings` singleton's ``__dict__``; undo that after every test
    (not just the ones that call it) so it never leaks into an unrelated test sharing the same process."""
    yield
    for key in ("etsy_enabled", "etsy_shop_id", "etsy_timeout_s"):
        fresh_company.__dict__.pop(key, None)


@pytest.fixture
def api(monkeypatch, fresh_company):
    """A fake Etsy API (net_guard-pinned) plus a connected OAuth token; records every request it served.
    ``state["receipts"]`` can be swapped between calls to simulate a later poll (new orders/refunds)."""
    _set_settings(fresh_company, etsy_enabled=True, etsy_shop_id=SHOP_ID)
    _connect_oauth(monkeypatch)

    state = {"receipts": fixture("receipts")}
    inventory = fixture("listing_inventory")
    calls: list[dict] = []

    def _check_headers(headers: dict) -> None:
        assert headers["x-api-key"] == CLIENT_ID
        assert headers["Authorization"] == "Bearer " + ACCESS_TOKEN

    def fake_get(target, *, headers=None, timeout=15):
        calls.append({"method": "GET", "url": target})
        _check_headers(headers)
        path = urlsplit(target).path
        if path == f"/v3/application/shops/{SHOP_ID}/listings/active":
            return _response("GET", target, json=fixture("listings_active"))
        if path == f"/v3/application/shops/{SHOP_ID}/receipts":
            return _response("GET", target, json=state["receipts"])
        if path.startswith("/v3/application/listings/") and path.endswith("/inventory"):
            listing_id = path.split("/")[4]
            if listing_id in inventory:
                return _response("GET", target, json=inventory[listing_id])
        return _response("GET", target, status=404, json={"error": "not found"})

    def fake_post(target, *, data=None, json=None, headers=None, timeout=15):
        calls.append({"method": "POST", "url": target, "data": data, "json": json})
        _check_headers(headers)
        path = urlsplit(target).path
        if path == f"/v3/application/shops/{SHOP_ID}/listings":
            assert data is not None and json is None  # Etsy's create-listing body is form-encoded, not JSON
            return _response("POST", target, status=201, json=fixture("create_listing_response"))
        return _response("POST", target, status=404, json={"error": "not found"})

    def fake_put(target, *, json=None, headers=None, timeout=15):
        calls.append({"method": "PUT", "url": target, "json": json})
        _check_headers(headers)
        return _response("PUT", target, json={"products": json["products"]})

    monkeypatch.setattr(etsy, "resolve_public", lambda url: url)  # identity: target is the bare url
    monkeypatch.setattr(etsy, "pinned_get", fake_get)
    monkeypatch.setattr(etsy, "pinned_post", fake_post)
    monkeypatch.setattr(etsy, "_pinned_put", fake_put)

    channel = etsy.EtsyChannel()
    channel.calls, channel.state = calls, state
    return channel


def counts() -> dict[str, int]:
    from sqlalchemy import func

    with session() as s:
        return {m.__name__: s.scalar(select(func.count()).select_from(m)) or 0
                for m in (Order, Product, Refund, LicenseKey)}


def order_lines(order_id: str):
    from aecom.db import LedgerEntry

    with session() as s:
        return list(s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order_id).order_by(LedgerEntry.id)))


def make_shared_product(keys: int = 1) -> None:
    """A licence product also sold on the primary channel, with `keys` available keys - the shared pool."""
    store = get_store()
    store.create_product("LIC-SHARED-001", "FocusFlow Pro", 50.0, "license")
    store.add_license_keys("LIC-SHARED-001", [f"FOCUSFLOW-{i:04d}" for i in range(keys)])
    store.set_status("LIC-SHARED-001", "active")


# --------------------------------------------------------------------------- channel registry (disabled by default)
def test_etsy_disabled_by_default(fresh_company):
    assert channels.enabled_channels() == ["primary"]
    assert isinstance(channels.get_channel("primary"), type(get_store()))


def test_etsy_enabled_is_an_opt_in_setting(fresh_company):
    _set_settings(fresh_company, etsy_enabled=True)
    assert channels.enabled_channels() == ["primary", "etsy"]
    assert isinstance(channels.get_channel("etsy"), etsy.EtsyChannel)


def test_unregistered_channel_name_is_rejected(fresh_company):
    with pytest.raises(NotImplementedError):
        channels.get_channel("shopify")


# --------------------------------------------------------------------------- sync_listings (read-only)
def test_sync_listings_maps_skus_without_touching_the_primary_catalog(api):
    make_shared_product()
    r = api.sync_listings()
    assert r == {"listings": 2, "mapped": 1, "unmapped": 1}
    mapping = json.loads(get_setting(etsy.LISTING_MAP_SETTING))
    assert mapping == {"111222333": "LIC-SHARED-001", "444555666": "ETSY-ONLY-002"}
    with session() as s:
        rows = list(s.scalars(select(Product).where(Product.sku == "LIC-SHARED-001")))
        assert len(rows) == 1 and rows[0].kind == "license"  # never duplicated or overwritten
        assert s.scalars(select(Product).where(Product.sku == "ETSY-ONLY-002")).first() is None  # never pre-created


# --------------------------------------------------------------------------- sync_orders: new paid receipts
def test_sync_orders_books_net_of_marketplace_handled_vat_and_fills_catalog_gap(api):
    make_shared_product()
    before = counts()
    r = api.sync_orders(SINCE)
    assert r == {"receipts": 2, "created": 2, "updated": 0, "unchanged": 0, "ignored": 0, "errors": 0}
    assert counts()["Order"] - before["Order"] == 2
    with session() as s:
        shared = s.scalars(select(Order).where(Order.external_id == "etsy:9001001234")).one()
        fallback = s.scalars(select(Order).where(Order.external_id == "etsy:9001005678")).one()
        gap = s.get(Product, fallback.product_id)
    # EUR 50.00 gross, EUR 10.00 Etsy-collected VAT -> booked net at EUR 40.00, no local VAT line
    assert (shared.amount_eur, shared.vat_eur, shared.status, shared.campaign_id) == (40.0, 0.0, "paid", "etsy")
    lines = [(e.account, e.debit, e.credit) for e in order_lines(shared.id)]
    assert lines == [("cash", 40.0, 0.0), ("revenue", 0.0, 40.0)]
    assert "excl. Etsy VAT EUR 10.00" in order_lines(shared.id)[0].memo
    # no tax breakdown in the fixture -> the 'otherwise mark vat as marketplace-handled' fallback: full gross booked
    assert (fallback.amount_eur, fallback.vat_eur, fallback.status) == (15.0, 0.0, "paid")
    assert "marketplace-handled)" in order_lines(fallback.id)[0].memo
    assert (gap.sku, gap.kind, gap.status) == ("ETSY-ONLY-002", "download", "active")  # gap filled, never overwritten
    drain()
    with session() as s:
        shared, fallback = s.get(Order, shared.id), s.get(Order, fallback.id)
        assert (shared.status, shared.email_sent) == ("delivered", True)
        assert (fallback.status, fallback.email_sent) == ("delivered", True)
        assert s.scalars(select(OutboxEmail).where(OutboxEmail.to == "mia@example.de")).first() is not None
        assert s.scalars(select(OutboxEmail).where(OutboxEmail.to == "sam@example.com")).first() is not None
    assert check_all() == []


def test_sync_orders_is_idempotent_on_receipt_id(api):
    make_shared_product()
    api.sync_orders(SINCE)
    drain()
    before = counts()
    r = api.sync_orders(SINCE)
    assert r == {"receipts": 2, "created": 0, "updated": 0, "unchanged": 2, "ignored": 0, "errors": 0}
    assert counts() == before
    assert check_all() == []


def test_sync_orders_ignores_receipts_not_paid_yet(api):
    api.state["receipts"] = fixture("receipts_unpaid")
    before = counts()
    r = api.sync_orders(SINCE)
    assert r == {"receipts": 1, "created": 0, "updated": 0, "unchanged": 0, "ignored": 1, "errors": 0}
    assert counts() == before


# --------------------------------------------------------------------------- refunds mirrored via the existing path
def test_refund_mirrored_scaled_and_idempotent_then_full_revokes_key(api):
    make_shared_product()
    api.sync_orders(SINCE)
    drain()
    with session() as s:
        order_id = s.scalars(select(Order.id).where(Order.external_id == "etsy:9001001234")).one()

    api.state["receipts"] = fixture("receipts_refund_partial")
    partial = api.sync_orders(SINCE)
    assert partial["updated"] == 1
    with session() as s:
        o = s.get(Order, order_id)
        refunds = list(s.scalars(select(Refund).where(Refund.order_id == order_id)))
    # EUR 12.50 refunded gross on Etsy, scaled by the order's net/gross ratio (40/50 = 0.8) -> EUR 10.00 net
    assert [r.amount_eur for r in refunds] == [10.0]
    assert o.status == "partially_refunded"

    before = counts()
    again = api.sync_orders(SINCE)  # same partial fixture again: nothing new to book
    assert again["updated"] == 0 and again["unchanged"] == 1
    assert counts() == before

    api.state["receipts"] = fixture("receipts_refund_full")
    full = api.sync_orders(SINCE)
    assert full["updated"] == 1
    with session() as s:
        o = s.get(Order, order_id)
        refunds = sorted(r.amount_eur for r in s.scalars(select(Refund).where(Refund.order_id == order_id)))
        key = s.scalars(select(LicenseKey).where(LicenseKey.order_id == order_id)).first()
    assert refunds == [10.0, 30.0] and o.status == "refunded"
    assert key.status == "revoked"  # MockStore.refund_order's existing full-refund behaviour, reused as-is
    assert check_all() == []


# --------------------------------------------------------------------------- shared licence-key pool (F3's point)
def test_shared_key_pool_no_double_allocation_across_channels(api):
    make_shared_product(keys=2)
    primary = get_store().create_order("primary-buyer@example.com", "LIC-SHARED-001", "FR")
    api.sync_orders(SINCE)
    drain()
    with session() as s:
        etsy_order_id = s.scalars(select(Order.id).where(Order.external_id == "etsy:9001001234")).one()
        product_id = s.scalars(select(Product.id).where(Product.sku == "LIC-SHARED-001")).one()
        # only the keys actually handed out - low stock after the second delivery also triggers the existing
        # restock reflex (I4/handlers.restock_low), which mints further *available* (order_id is None) keys here
        keys = {k.order_id: k.key for k in s.scalars(select(LicenseKey).where(LicenseKey.product_id == product_id,
                LicenseKey.order_id.is_not(None)))}
    assert set(keys) == {primary["id"], etsy_order_id}
    assert keys[primary["id"]] != keys[etsy_order_id]  # two distinct keys from the one pool, never the same twice
    assert no_key_on_two_orders() == []
    assert check_all() == []


def test_shared_key_pool_out_of_stock_fails_the_second_order_not_a_double_allocation(api, fresh_company, monkeypatch):
    # the auto-restock reflex (default license_key_source=mock) would otherwise mask a genuine stock-out by
    # minting more keys as soon as availability dips low; turn it off so this order really stays out of stock
    monkeypatch.setattr(fresh_company, "license_key_source", "none")
    make_shared_product(keys=1)  # only one key: the primary order takes it, the etsy order must not also get it
    get_store().create_order("primary-buyer@example.com", "LIC-SHARED-001", "FR")
    api.sync_orders(SINCE)
    drain()
    with session() as s:
        etsy_order = s.scalars(select(Order).where(Order.external_id == "etsy:9001001234")).one()
    assert etsy_order.status == "failed"  # out of stock, not a second key handed out
    assert no_key_on_two_orders() == []
    assert check_all() == []


# --------------------------------------------------------------------------- OAuth failure: no partial writes
def test_oauth_failure_blocks_sync_before_any_write(monkeypatch, fresh_company):
    _set_settings(fresh_company, etsy_enabled=True, etsy_shop_id=SHOP_ID)
    monkeypatch.setenv("ETSY_CLIENT_ID", CLIENT_ID)  # connected to the API, but never to OAuth (no refresh token)
    channel = etsy.EtsyChannel()
    before = counts()
    with pytest.raises(OAuthError):
        channel.sync_listings()
    with pytest.raises(OAuthError):
        channel.sync_orders(SINCE)
    assert counts() == before
    assert get_setting(etsy.LISTING_MAP_SETTING) is None


# --------------------------------------------------------------------------- writes: side-effecting, never automatic
def test_create_listing_and_update_inventory_are_plain_callables_nothing_calls_automatically(api):
    r = api.create_listing("NEW-SKU-1", "New Draft Product", "A newly created draft listing.", 25.0)
    assert r == {"listing_id": "999888777", "sku": "NEW-SKU-1", "state": "draft"}
    assert json.loads(get_setting(etsy.LISTING_MAP_SETTING))["999888777"] == "NEW-SKU-1"
    create_call = next(c for c in api.calls if c["method"] == "POST")
    assert create_call["data"]["title"] == "New Draft Product" and create_call["json"] is None

    r2 = api.update_listing_inventory("NEW-SKU-1", 42)
    assert r2 == {"listing_id": "999888777", "sku": "NEW-SKU-1", "quantity": 42}
    put_call = next(c for c in api.calls if c["method"] == "PUT")
    assert put_call["json"]["products"][0]["offerings"][0]["quantity"] == 42

    with pytest.raises(ValueError, match="no Etsy listing is mapped"):
        api.update_listing_inventory("SOME-OTHER-SKU", 1)
