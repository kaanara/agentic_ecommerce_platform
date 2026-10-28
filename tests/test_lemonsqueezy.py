"""A5 / L1: Lemon Squeezy adapter. Offline: documented payload shapes from tests/fixtures/lemonsqueezy + MockTransport."""

import copy
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from aecom import db
from aecom.bus.events import HandlerFailed, drain
from aecom.chaos.invariants import check_all
from aecom.db import AuditLog, Customer, ErrorRow, EventRow, LedgerEntry, LicenseKey, Order, Product, Refund, session
from aecom.store import lemonsqueezy as ls
from aecom.store.adapter import get_store

FIX = Path(__file__).parent / "fixtures" / "lemonsqueezy"
FULL_KEY = "TEST0000-aaaa-4bbb-8ccc-fake00009f3c"


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture
def api(monkeypatch, fresh_company):
    """A fake Lemon Squeezy API; records the requests it served."""
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", "test-ls-key")
    monkeypatch.setattr(fresh_company, "lemonsqueezy_store_id", "7")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer test-ls-key"
        assert request.headers["Accept"] == "application/vnd.api+json"
        path = request.url.path
        if path == "/v1/products":
            assert request.url.params["filter[store_id]"] == "7"
            return httpx.Response(200, json=fixture("products"))
        if path == "/v1/variants":
            body = fixture("variants")
            pid = int(request.url.params["filter[product_id]"])
            body["data"] = [v for v in body["data"] if v["attributes"]["product_id"] == pid]
            return httpx.Response(200, json=body)
        if path == "/v1/orders/5001/refund" and request.method == "POST":
            sent = json.loads(request.content)
            assert sent == {"data": {"type": "orders", "id": "5001", "attributes": {"amount": 1000}}}
            return httpx.Response(200, json=fixture("refund_response"))
        return httpx.Response(404, json={"errors": [{"detail": "not found"}]})

    store = ls.LemonSqueezyStore(transport=httpx.MockTransport(handler))
    store.calls = calls
    return store


def counts() -> dict[str, int]:
    with session() as s:
        return {m.__name__: s.scalar(select(func.count()).select_from(m)) or 0
                for m in (Order, Customer, LedgerEntry, Refund, EventRow, LicenseKey, Product)}


def order_lines(order_id: str) -> list[LedgerEntry]:
    with session() as s:
        return list(s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order_id).order_by(LedgerEntry.id)))


def balance(order_id: str, account: str) -> int:
    """Credit-minus-debit in cents for one account of one order."""
    return sum(round(e.credit * 100) - round(e.debit * 100) for e in order_lines(order_id) if e.account == account)


def test_sync_catalog_maps_variants_to_local_products(api):
    r = api.sync_catalog()
    assert r == {"products": 3, "variants": 4, "created": 4, "updated": 0}
    by_ext = {p["sku"]: p for p in api.list_products()}
    lic = by_ext["LS-101"]
    assert (lic["title"], lic["kind"], lic["price_eur"], lic["status"]) == ("FocusFlow Pro", "license", 49.0, "active")
    course = by_ext["LS-201"]
    assert (course["title"], course["kind"], course["status"]) == ("AI Automation Course - Self-paced", "download", "active")
    assert by_ext["LS-202"]["status"] == "draft"  # draft variant of a published product
    assert by_ext["LS-301"]["status"] == "draft"  # variant of a draft product
    with session() as s:
        assert s.scalars(select(Product.external_id).where(Product.sku == "LS-202")).one() == "202"
        s.scalars(select(Product).where(Product.sku == "LS-101")).one().sku = "LIC-FOCUSFLOW"  # local rename survives
    again = api.sync_catalog()
    assert (again["created"], again["updated"]) == (0, 4)
    assert api.get_product("LIC-FOCUSFLOW")["price_eur"] == 49.0
    assert api.get_product("LS-101") is None
    # licence keys come from Lemon Squeezy per purchase: no local stock to run out of
    assert all(i["available_keys"] is None for i in api.inventory())


def test_mirror_order_books_lemon_squeezy_tax_delivered_and_emailed(api):
    api.sync_catalog()
    before = counts()
    r = ls.mirror_order(fixture("order_created"))
    assert r["external_id"] == "5001" and r["status"] == "delivered"
    with session() as s:
        o = s.get(Order, r["order_id"])
        c = s.get(Customer, o.customer_id)
        p = s.get(Product, o.product_id)
        events = [(e.topic, e.source) for e in s.scalars(select(EventRow).where(EventRow.id > 0))]
    assert (o.amount_eur, o.vat_eur, o.status, o.email_sent) == (49.0, 8.5, "delivered", True)
    assert o.email_sent_at is not None and p.external_id == "101"
    assert (c.email, c.country, c.name) == ("eva@example.nl", "NL", "Eva de Vries")
    assert o.withdrawal_consent_version == "2026-10" and str(o.withdrawal_consent_at) == "2026-10-27 09:59:30"
    lines = [(e.account, e.debit, e.credit, e.country, e.vat_rate) for e in order_lines(o.id)]
    assert lines == [("cash", 49.0, 0.0, None, None), ("revenue", 0.0, 40.5, None, None),
                     ("vat_payable", 0.0, 8.5, "NL", 0.21)]
    assert sum(round(d * 100) for _, d, *_ in lines) == sum(round(c * 100) for _, _, c, *_ in lines)
    assert ("order.mirrored", "lemonsqueezy") in events and ("order.paid", "store") not in events
    after = counts()
    assert after["EventRow"] - before["EventRow"] == 1 and after["Order"] - before["Order"] == 1
    drain()
    assert check_all() == []


def test_second_mirror_of_same_order_changes_nothing(api):
    api.sync_catalog()
    first = ls.mirror_order(fixture("order_created"))
    before = counts()
    second = ls.mirror_order(fixture("order_created"))
    assert second["already"] is True and second["order_id"] == first["order_id"]
    assert counts() == before


def test_mirror_order_without_custom_data_leaves_country_and_consent_open(fresh_company):
    payload = fixture("order_created")
    payload["meta"].pop("custom_data")
    r = ls.mirror_order(payload)  # product not synced yet: a placeholder keeps the sale
    with session() as s:
        o = s.get(Order, r["order_id"])
        assert o.withdrawal_consent_at is None and o.withdrawal_consent_version is None
        assert s.get(Customer, o.customer_id).country == ""
        assert s.get(Product, o.product_id).external_id == "101"
    vat = [e for e in order_lines(r["order_id"]) if e.account == "vat_payable"]
    assert [(e.credit, e.country, e.vat_rate) for e in vat] == [(8.5, None, 0.21)]
    assert ls.mirror_order(payload, country="nl")["already"] is True


def test_non_paid_and_foreign_currency_orders_are_not_booked(fresh_company):
    pending = fixture("order_created")
    pending["data"]["attributes"]["status"] = "pending"
    before = counts()
    assert ls.mirror_order(pending)["status"] == "ignored"
    usd = fixture("order_created")
    usd["data"]["attributes"]["currency"] = "USD"
    with pytest.raises(ValueError, match="USD"):
        ls.mirror_order(usd)
    assert counts() == before


def test_refund_mirror_reverses_vat_partial_then_full_idempotent(api):
    api.sync_catalog()
    oid = ls.mirror_order(fixture("order_created"))["order_id"]
    partial = ls.mirror_refund(fixture("order_refunded_partial"))
    assert (partial["status"], partial["refunded_eur"], partial["remaining_eur"]) == ("partially_refunded", 19.0, 30.0)
    # VAT reversed pro rata on Lemon Squeezy's tax: 8.50 * 19/49 = 3.30
    assert balance(oid, "vat_payable") == 850 - 330
    snapshot = counts()
    assert ls.mirror_refund(fixture("order_refunded_partial"))["already"] is True
    assert counts() == snapshot
    full = ls.mirror_refund(fixture("order_refunded_full"))
    assert (full["status"], full["refunded_eur"], full["remaining_eur"]) == ("refunded", 30.0, 0.0)
    assert balance(oid, "vat_payable") == 0 and balance(oid, "cash") == 0
    with session() as s:
        assert sorted(r.amount_eur for r in s.scalars(select(Refund).where(Refund.order_id == oid))) == [19.0, 30.0]
        assert s.get(Order, oid).status == "refunded"
    snapshot = counts()
    assert ls.mirror_refund(fixture("order_refunded_full"))["already"] is True
    assert ls.mirror_refund(fixture("order_refunded_partial"))["already"] is True  # late, out-of-order replay
    assert counts() == snapshot
    drain()
    assert check_all() == []


def test_refund_before_order_created_mirrors_the_sale_first(fresh_company):
    r = ls.mirror_refund(fixture("order_refunded_full"))
    assert r["status"] == "refunded"
    assert balance(r["order_id"], "vat_payable") == 0
    assert ls.mirror_order(fixture("order_created"))["already"] is True
    with session() as s:
        assert [e.topic for e in s.scalars(select(EventRow).where(EventRow.source == "lemonsqueezy"))] == [
            "order.mirrored", "order.refunded"]
    drain()
    assert check_all() == []


def test_licence_key_never_stored_in_full(api):
    with pytest.raises(HandlerFailed):  # sent alongside order_created; retried until the order is mirrored
        ls.mirror_license_key(fixture("license_key_created"))
    api.sync_catalog()
    oid = ls.mirror_order(fixture("order_created"))["order_id"]
    r = ls.mirror_license_key(fixture("license_key_created"))
    assert FULL_KEY not in json.dumps(r)
    assert (r["key_last4"], r["status"], r["already"]) == ("9f3c", "assigned", False)
    assert ls.mirror_license_key(fixture("license_key_created"))["already"] is True
    with session() as s:
        keys = list(s.scalars(select(LicenseKey).where(LicenseKey.order_id == oid)))
        assert [(k.key, k.status) for k in keys] == [("ls:3001:****9f3c", "assigned")]
        assert s.get(Order, oid).quantity == 1
    # the full key appears nowhere in the database
    engine = db.get_engine()
    with engine.connect() as conn:
        for table in db.Base.metadata.sorted_tables:
            for row in conn.execute(table.select()):
                assert FULL_KEY not in json.dumps([str(v) for v in row])
    ls.mirror_refund(fixture("order_refunded_full"))
    with session() as s:
        assert s.scalars(select(LicenseKey.status).where(LicenseKey.order_id == oid)).one() == "revoked"
    drain()
    assert check_all() == []


def test_adapter_delivery_is_a_noop_and_catalog_writes_point_to_the_dashboard(api, fresh_company, monkeypatch):
    monkeypatch.setattr(fresh_company, "store_adapter", "lemonsqueezy")
    assert isinstance(get_store(), ls.LemonSqueezyStore)
    api.sync_catalog()
    oid = ls.mirror_order(fixture("order_created"))["order_id"]
    snapshot = counts()
    assert api.deliver_order(oid) == {"order_id": oid, "status": "delivered", "already": True,
                                      "delivered_by": "lemonsqueezy"}
    assert counts() == snapshot
    assert api.get_order(oid)["external_id"] == "5001" and api.get_order(oid)["email_sent"] is True
    for call in (lambda: api.create_product("X", "X", 1.0, "license"), lambda: api.update_price("LS-101", 59.0),
                 lambda: api.set_status("LS-101", "active")):
        with pytest.raises(NotImplementedError, match="Lemon Squeezy dashboard"):
            call()
    with pytest.raises(NotImplementedError):
        api.create_order("a@example.com", "LS-101")


def test_refund_order_calls_the_refund_endpoint_and_mirrors_it(api):
    api.sync_catalog()
    oid = ls.mirror_order(fixture("order_created"))["order_id"]
    with pytest.raises(ValueError, match="exceeds"):
        api.refund_order(oid, 49.01, "too much")
    r = api.refund_order(oid, 10.0, "customer asked")
    assert (r["status"], r["refunded_eur"], r["remaining_eur"]) == ("partially_refunded", 10.0, 39.0)
    assert [c.method for c in api.calls if c.url.path.endswith("/refund")] == ["POST"]
    # the webhook reports 19.00 refunded in total: only the 9.00 not yet mirrored is booked
    assert ls.mirror_refund(fixture("order_refunded_partial"))["refunded_eur"] == 9.0
    with session() as s:
        assert s.scalars(select(Refund.reason).where(Refund.order_id == oid).order_by(Refund.amount_eur.desc())).first() \
            == "customer asked"
    assert balance(oid, "vat_payable") == 850 - 330


def test_webhook_signature_and_dispatch():
    body = json.dumps(fixture("order_created")).encode()
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert ls.verify_signature(body, sig, secret="whsec")
    assert not ls.verify_signature(body + b" ", sig, secret="whsec")
    assert not ls.verify_signature(body, sig, secret="")
    assert not ls.verify_signature(body, None, secret="whsec")
    assert ls.dispatch({"meta": {"event_name": "subscription_created"}, "data": {}}) == {
        "event": "subscription_created", "status": "ignored"}
    assert ls.dispatch(copy.deepcopy(fixture("order_created")))["status"] == "delivered"


def key_payload(key_id: int, last4: str = "0001") -> dict:
    p = copy.deepcopy(fixture("license_key_created"))
    p["data"]["id"] = str(key_id)
    p["data"]["attributes"]["key"] = f"TEST0000-aaaa-4bbb-8ccc-fake0000{last4}"
    return p


def test_mirror_order_takes_quantity_from_the_order_item(api):
    api.sync_catalog()
    p = fixture("order_created")
    p["data"]["attributes"]["first_order_item"]["quantity"] = 2
    oid = ls.mirror_order(p)["order_id"]
    ls.mirror_license_key(key_payload(3001, "0001"))
    ls.mirror_license_key(key_payload(3002, "0002"))
    with session() as s:
        assert s.get(Order, oid).quantity == 2
        assert s.scalar(select(func.count(LicenseKey.id)).where(LicenseKey.order_id == oid)) == 2
    assert check_all() == []


def test_licence_key_beyond_the_quantity_is_rejected_and_audited(api):
    api.sync_catalog()
    oid = ls.mirror_order(fixture("order_created"))["order_id"]  # quantity 1
    ls.mirror_license_key(key_payload(3001))
    r = ls.mirror_license_key(key_payload(3002, "0002"))
    assert (r["status"], r["reason"]) == ("rejected", "license_key_over_quantity")
    with session() as s:
        assert s.get(Order, oid).quantity == 1
        assert s.scalars(select(LicenseKey.key).where(LicenseKey.order_id == oid)).all() == ["ls:3001:****0001"]
        assert s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "license_key_over_quantity")) == 1
        assert s.scalars(select(ErrorRow.component)).all() == ["lemonsqueezy.license_key"]
    assert ls.mirror_license_key(key_payload(3001))["already"] is True  # the stored key stays idempotent
    assert check_all() == []


def test_mirror_functions_write_into_the_callers_session(fresh_company):
    with session() as s:
        ls.dispatch(fixture("order_created"), session=s)
        assert s.scalar(select(func.count(Order.id))) == 1
        s.rollback()  # nothing was committed behind the caller's back
    assert counts()["Order"] == 0 and counts()["EventRow"] == 0


def test_reconcile_recent_recovers_a_lost_licence_key_once(api):
    api.sync_catalog()
    ls.mirror_order(fixture("order_created"))  # its license_key_created webhook was lost
    seen = []
    key = fixture("license_key_created")["data"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("filter[order_id]"))
        return httpx.Response(200, json={"data": [key], "meta": {"page": {"lastPage": 1}}})

    api._transport = httpx.MockTransport(handler)
    api.recent_orders = lambda since: [fixture("order_created")["data"]]
    since = ls._ts("2026-10-26T00:00:00Z")
    first = api.reconcile_recent(since)
    assert (first["keys"], first["key_errors"]) == (1, 0) and seen == ["5001"]
    with session() as s:
        assert s.scalars(select(LicenseKey.key)).all() == ["ls:3001:****9f3c"]
    second = api.reconcile_recent(since)
    assert second["keys"] == 0 and seen == ["5001"]  # nothing missing any more: no API call
    assert check_all() == []
