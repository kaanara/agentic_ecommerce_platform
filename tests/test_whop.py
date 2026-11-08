"""Whop adapter. Offline: shapes from tests/fixtures/whop + MockTransport."""

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aecom.api import app as api_app
from aecom.api import webhooks
from aecom.bus.events import drain
from aecom.chaos.invariants import check_all
from aecom.db import Customer, ErrorRow, LedgerEntry, Order, Product, Refund, WebhookReceipt, session
from aecom.reconciler import reconcile
from aecom.store import whop
from aecom.store.adapter import get_store

FIX = Path(__file__).parent / "fixtures" / "whop"
PAY = "pay_0000000000001"
SECRET = "ws_" + base64.b64encode(b"whop-test-key-0123456789").decode()


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture
def api(monkeypatch, fresh_company):
    """A fake Whop API; records the requests it served."""
    monkeypatch.setenv("WHOP_API_KEY", "whop-test-key")
    monkeypatch.setattr(fresh_company, "whop_account_id", "biz_store")
    calls: list[httpx.Request] = []
    state: dict = {"payment": fixture("payment_partially_refunded"), "payments": [fixture("payment_succeeded")["data"]],
                   "created": {}, "refund_status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer whop-test-key"
        assert request.headers["Api-Version-Date"] == fresh_company.whop_api_version
        path, method = request.url.path, request.method
        if method != "GET":
            assert request.headers["Idempotency-Key"].startswith("aecom-")
        body = json.loads(request.content) if request.content else None
        if path == "/api/v1/products" and method == "GET":
            return httpx.Response(200, json=fixture("products"))
        if path == "/api/v1/variants" and method == "GET":
            return httpx.Response(200, json=fixture("variants"))
        if path == "/api/v1/payments" and method == "GET":
            return httpx.Response(200, json={"data": state["payments"], "page_info": {"has_next_page": False}})
        if path == f"/api/v1/payments/{PAY}" and method == "GET":
            return httpx.Response(200, json=state["payment"])
        if path == f"/api/v1/payments/{PAY}/refund":
            if state["refund_status"] == 409:
                return httpx.Response(409, json={"error": {"type": "conflict", "message": "already refunded"}})
            return httpx.Response(200, json=state["payment"])
        if path == "/api/v1/products" and method == "POST":
            state["created"]["product"] = body
            return httpx.Response(200, json={"id": "prod_new", "title": body["title"], "visibility": "hidden"})
        if path == "/api/v1/variants" and method == "POST":
            state["created"]["variant"] = body
            return httpx.Response(200, json={"id": "plan_new", "purchase_url": "https://whop.com/checkout/plan_new"})
        if path.startswith("/api/v1/variants/plan_") and method == "PATCH":
            state.setdefault("patched", []).append((path, body))
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "product": {"id": "prod_new"}, **body})
        if path.startswith("/api/v1/products/prod_") and method == "PATCH":
            state.setdefault("patched", []).append((path, body))
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], **body})
        return httpx.Response(404, json={"error": {"type": "not_found", "message": "not found"}})

    store = whop.WhopStore(transport=httpx.MockTransport(handler))
    store.calls, store.state = calls, state
    return store


def counts() -> dict[str, int]:
    with session() as s:
        return {m.__name__: s.scalar(select(func.count()).select_from(m)) or 0
                for m in (Order, Customer, LedgerEntry, Refund, Product)}


def lines(order_id: str) -> list[tuple]:
    with session() as s:
        return [(e.account, e.debit, e.credit, e.country, e.vat_rate)
                for e in s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order_id).order_by(LedgerEntry.id))]


# --------------------------------------------------------------------------- catalog
def test_sync_catalog_maps_variants(api):
    assert api.sync_catalog() == {"products": 4, "variants": 4, "created": 4, "updated": 0, "duplicate_skus": []}
    by_sku = {p["sku"]: p for p in api.list_products()}
    lic = by_sku["LIC-FOCUSFLOW"]  # the variant's own sku is kept
    assert (lic["title"], lic["price_eur"], lic["status"]) == ("FocusFlow Pro", 49.0, "active")
    assert by_sku["WH-plan_301"]["kind"] == "subscription" and by_sku["WH-plan_301"]["price_eur"] == 9.0
    assert by_sku["WH-plan_401"]["status"] == "draft"
    assert api.sync_catalog()["updated"] == 4


def test_get_store_returns_whop(monkeypatch, fresh_company):
    monkeypatch.setattr(fresh_company, "store_adapter", "whop")
    assert isinstance(get_store(), whop.WhopStore)


def test_create_product_makes_hidden_product_and_variant(api):
    r = api.create_product("NEW-TOOL-V1", "New Tool", 19.0, "license", "A tool")
    created = api.state["created"]
    assert created["product"]["visibility"] == "hidden" and created["product"]["account_id"] == "biz_store"
    assert created["variant"]["product_id"] == "prod_new" and created["variant"]["visibility"] == "hidden"
    assert created["variant"]["initial_price"] == 19.0 and created["variant"]["billing_type"] == "one_time"
    assert r["whop_variant_id"] == "plan_new" and r["status"] == "draft"
    with session() as s:
        assert s.scalars(select(Product.external_id).where(Product.sku == "NEW-TOOL-V1")).one() == "plan_new"
    with pytest.raises(ValueError, match="already exists"):
        api.create_product("NEW-TOOL-V1", "x", 1.0, "license")


def test_update_price_and_status_write_to_whop_then_local(api):
    api.create_product("NEW-TOOL-V1", "New Tool", 19.0, "license")
    api.update_price("NEW-TOOL-V1", 25.0)
    assert api.state["patched"][-1] == ("/api/v1/variants/plan_new", {"initial_price": 25.0})
    assert api.get_product("NEW-TOOL-V1")["price_eur"] == 25.0
    api.set_status("NEW-TOOL-V1", "active")
    paths = [p for p, _ in api.state["patched"]]
    assert "/api/v1/variants/plan_new" in paths and "/api/v1/products/prod_new" in paths
    assert api.state["patched"][-2][1] == {"visibility": "visible"}
    assert api.get_product("NEW-TOOL-V1")["status"] == "active"
    with pytest.raises(ValueError, match="status must be"):
        api.set_status("NEW-TOOL-V1", "live")


def test_writes_on_a_non_whop_sku_are_refused(api):
    from aecom.store.mock import MockStore

    MockStore().create_product("LOCAL-1", "Local", 5.0, "license")
    with pytest.raises(ValueError, match="not a Whop product"):
        api.update_price("LOCAL-1", 6.0)
    with pytest.raises(NotImplementedError):
        api.add_license_keys("LOCAL-1", ["k"])
    with pytest.raises(NotImplementedError, match="checkout"):
        api.create_order("a@b.c", "LOCAL-1")


def test_sync_catalog_survives_two_variants_with_one_sku(api, monkeypatch):
    data = fixture("variants")
    data["data"][1]["sku"] = "LIC-FOCUSFLOW"  # a duplicate listing reuses the SKU of plan_101
    real = api.pages
    monkeypatch.setattr(api, "pages", lambda path, *a, **k: data["data"] if path == "/variants" else real(path, *a, **k))
    r = api.sync_catalog()
    assert r["created"] == 4
    assert r["duplicate_skus"] == [{"sku": "LIC-FOCUSFLOW", "variant_id": "plan_201", "imported_as": "WH-plan_201"}]
    skus = {p["sku"] for p in api.list_products()}
    assert {"LIC-FOCUSFLOW", "WH-plan_201"} <= skus


def test_api_error_carries_whops_message(api):
    with pytest.raises(whop.WhopError, match="HTTP 404: not found") as exc:
        api.request("GET", "/nothing")
    assert exc.value.status == 404 and exc.value.expected  # an agent's wrong id is not an ops error


# --------------------------------------------------------------------------- mirroring
def test_mirror_payment_books_whop_tax_delivered_and_emailed(api):
    api.sync_catalog()
    r = whop.mirror_payment(fixture("payment_succeeded"))
    assert r["external_id"] == PAY and r["status"] == "delivered"
    with session() as s:
        o = s.get(Order, r["order_id"])
        c = s.get(Customer, o.customer_id)
        assert (o.amount_eur, o.vat_eur, o.email_sent, o.campaign_id) == (49.0, 8.5, True, "c-1")
        assert s.get(Product, o.product_id).external_id == "plan_101"
        assert (c.email, c.country, c.name, c.marketing_consent) == ("eva@example.nl", "NL", "Eva de Vries", True)
        assert o.withdrawal_consent_version == "2026-10"
    got = lines(r["order_id"])
    assert got[0] == ("cash", 49.0, 0.0, None, None) and got[1][:3] == ("revenue", 0.0, 40.5)
    assert got[2][:4] == ("vat_payable", 0.0, 8.5, "NL") and round(got[2][4], 3) == 0.21
    drain()
    assert check_all() == []


def test_mirror_is_idempotent_and_ignores_unpaid(fresh_company):
    first = whop.mirror_payment(fixture("payment_succeeded"))  # variant not synced: placeholder keeps the sale
    before = counts()
    assert whop.mirror_payment(fixture("payment_succeeded"))["already"] is True
    assert counts() == before
    open_ = fixture("payment_succeeded")
    open_["data"].update(id="pay_other", status="open")
    assert whop.mirror_payment(open_)["status"] == "ignored"
    assert counts() == before and first["order_id"]
    with session() as s:
        assert s.scalars(select(Product.sku)).all() == ["WH-plan_101"]


def test_foreign_currency_is_refused(fresh_company):
    usd = fixture("payment_succeeded")
    usd["data"]["currency"] = "usd"
    usd["data"]["total"]["currency"] = "usd"
    with pytest.raises(ValueError, match="not converted"):
        whop.mirror_payment(usd)


def test_partial_then_full_refund_book_only_the_difference(api):
    api.sync_catalog()
    whop.mirror_payment(fixture("payment_succeeded"))
    r = whop.mirror_refund({"data": fixture("payment_partially_refunded")})
    assert r["refunded_eur"] == 12.1 and r["status"] == "partially_refunded"
    assert whop.mirror_refund({"data": fixture("payment_partially_refunded")})["already"] is True
    r = whop.mirror_refund({"data": fixture("payment_refunded")})
    assert r["refunded_eur"] == 36.9 and r["status"] == "refunded" and r["remaining_eur"] == 0
    with session() as s:
        assert s.scalar(select(func.sum(Refund.amount_eur))) == pytest.approx(49.0)
    assert round(sum(c - d for a, d, c, *_ in lines(r["order_id"]) if a == "vat_payable"), 2) == 0.0
    drain()
    assert check_all() == []


def test_refund_arriving_first_mirrors_the_payment(fresh_company):
    assert whop.mirror_refund({"data": fixture("payment_refunded")})["status"] == "refunded"
    assert counts()["Order"] == 1


def test_dispatch_routes_and_ignores():
    assert whop.dispatch({"type": "membership.activated", "data": {}}) == {"event": "membership.activated",
                                                                         "status": "ignored"}
    pending = {"type": "refund.created", "data": {"status": "pending", "payment_id": PAY}}
    assert whop.dispatch(pending)["status"] == "ignored" and whop.refund_payment_id(pending) is None
    assert whop.refund_payment_id(fixture("refund_created")) == PAY


# --------------------------------------------------------------------------- API: reconcile and refunds
def test_reconcile_recent_mirrors_missing_payments_and_is_idempotent(api):
    api.sync_catalog()
    since = datetime(2026, 10, 1)
    r = api.reconcile_recent(since)
    assert (r["listed"], r["mirrored"], r["errors"]) == (1, 1, 0)
    again = api.reconcile_recent(since)
    assert (again["mirrored"], again["unchanged"]) == (0, 1)
    api.state["payments"] = [fixture("payment_partially_refunded")]
    assert api.reconcile_recent(since)["refunds"] == 1
    assert api.reconcile_recent(since)["refunds"] == 0
    query = [c.url.params for c in api.calls if c.url.path == "/api/v1/payments"][0]
    assert query["account_id"] == "biz_store" and query["statuses"] == "paid" and query["first"] == "100"


def test_reconcile_stops_at_payments_older_than_since(api):
    assert api.reconcile_recent(datetime(2026, 11, 1))["listed"] == 0


def test_refund_order_posts_partial_amount_and_mirrors(api):
    local = whop.mirror_payment(fixture("payment_succeeded"))["order_id"]
    r = api.refund_order(local, 12.10, "customer asked")
    sent = json.loads([c for c in api.calls if c.url.path.endswith("/refund")][0].content)
    assert sent["partial_amount"] == 12.1 and sent["metadata"] == {"reason": "customer asked"}
    assert r["status"] == "partially_refunded" and r["amount_eur"] == 12.10


def test_refund_conflict_rereads_and_mirrors(api):
    local = whop.mirror_payment(fixture("payment_succeeded"))["order_id"]
    api.state["refund_status"] = 409
    api.state["payment"] = fixture("payment_refunded")
    assert api.refund_order(local, 49.0, "dup")["status"] == "refunded"


def test_refund_validates_amount_and_order(api):
    local = whop.mirror_payment(fixture("payment_succeeded"))["order_id"]
    with pytest.raises(ValueError, match="exceeds"):
        api.refund_order(local, 99.0, "x")
    with pytest.raises(ValueError, match="not found"):
        api.refund_order("nope", 1.0, "x")


def test_reconciler_runs_whop_pass(api, fresh_company, monkeypatch):
    monkeypatch.setattr(fresh_company, "store_adapter", "whop")
    monkeypatch.setattr("aecom.store.adapter.get_store", lambda: api)
    api.state["payments"][0]["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = reconcile()
    assert r["whop_listed"] == 1 and r["whop_mirrored"] == 1


# --------------------------------------------------------------------------- signature
def sign(raw: bytes, key: bytes, msg_id="msg_1", ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": f"v1,bogus v1,{sig}",
            "content-type": "application/json"}


def std_key() -> bytes:
    return base64.b64decode(SECRET.removeprefix("ws_"))


def test_signature_accepts_both_key_forms_and_rejects_the_rest():
    raw = b'{"type":"x"}'
    for key in (std_key(), SECRET.encode()):
        h = sign(raw, key)
        assert whop.verify_signature(raw, h["webhook-id"], h["webhook-timestamp"], h["webhook-signature"], SECRET)
    h = sign(raw, std_key())
    args = (h["webhook-id"], h["webhook-timestamp"], h["webhook-signature"])
    assert not whop.verify_signature(raw + b" ", *args, SECRET)
    assert not whop.verify_signature(raw, *args, "ws_" + base64.b64encode(b"other").decode())
    assert not whop.verify_signature(raw, None, *args[1:], SECRET) and not whop.verify_signature(raw, *args, "")
    old = sign(raw, std_key(), ts=int(time.time()) - 6 * 60)
    assert not whop.verify_signature(raw, old["webhook-id"], old["webhook-timestamp"], old["webhook-signature"], SECRET)


# --------------------------------------------------------------------------- route
@pytest.fixture
def client(monkeypatch, fresh_company):
    configured = {"WHOP_WEBHOOK_SECRET": SECRET, "WHOP_API_KEY": "whop-test-key"}
    monkeypatch.setattr(webhooks.secrets, "get", lambda name: configured.get(name))
    c = TestClient(api_app.app)
    c.configured = configured
    return c


def post(client, payload: dict, **kw):
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/whop", content=raw, headers=sign(raw, std_key(), **kw))


def test_route_mirrors_once_and_dedupes_on_webhook_id(client):
    r = post(client, fixture("payment_succeeded"))
    assert r.status_code == 200 and r.json()["duplicate"] is False and r.json()["event"] == "payment.succeeded"
    assert post(client, fixture("payment_succeeded")).json()["duplicate"] is True
    assert counts()["Order"] == 1
    with session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookReceipt)) == 1


def test_route_refund_event_fetches_the_payment_first(client, monkeypatch):
    post(client, fixture("payment_succeeded"))
    fetched = []
    monkeypatch.setattr(whop.WhopStore, "get_payment",
                        lambda self, pid: fetched.append(pid) or fixture("payment_partially_refunded"))
    r = post(client, fixture("refund_created"), msg_id="msg_2")
    assert r.status_code == 200 and r.json()["refunded_eur"] == 12.1 and fetched == [PAY]
    with session() as s:
        assert s.scalars(select(Order.status)).one() == "partially_refunded"


def test_route_ignored_event_and_failed_refund_fetch_retries(client, monkeypatch):
    assert post(client, {"type": "membership.activated", "data": {}}, msg_id="m3").json()["status"] == "ignored"

    def down(self, pid):
        raise whop.WhopError("Whop is down")

    monkeypatch.setattr(whop.WhopStore, "get_payment", down)
    assert post(client, fixture("refund_created"), msg_id="m4").status_code == 500
    with session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookReceipt)) == 1  # only m3; m4 left no receipt
        assert "webhook.whop" in s.scalars(select(ErrorRow.component)).all()


def test_route_rejects_bad_signature_and_missing_secret(client):
    raw = json.dumps(fixture("payment_succeeded")).encode()
    assert client.post("/webhooks/whop", content=raw).status_code == 401
    assert client.post("/webhooks/whop", content=raw, headers=sign(raw, b"wrong")).status_code == 401
    assert counts()["Order"] == 0
    client.configured.pop("WHOP_WEBHOOK_SECRET")
    assert client.post("/webhooks/whop", content=raw, headers=sign(raw, std_key())).status_code == 503


def test_route_failed_mirror_is_500_and_retry_succeeds(client, monkeypatch):
    real = whop.mirror_payment
    monkeypatch.setattr(whop, "mirror_payment", lambda payload, session=None: 1 / 0)
    assert post(client, fixture("payment_succeeded")).status_code == 500
    with session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookReceipt)) == 0
    monkeypatch.setattr(whop, "mirror_payment", real)
    assert post(client, fixture("payment_succeeded")).json()["duplicate"] is False
    assert counts()["Order"] == 1
