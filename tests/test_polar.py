"""Polar adapter. Offline: documented payload shapes from tests/fixtures/polar + MockTransport."""

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
from aecom.store import polar
from aecom.store.adapter import get_store

FIX = Path(__file__).parent / "fixtures" / "polar"
ORDER_ID = "7c1f0a52-0000-4000-8000-000000005001"
SECRET = "whsec_" + base64.b64encode(b"polar-test-key-0123456789").decode()  # standard form
LEGACY_SECRET = "whsec_legacy-polar-secret"  # not valid base64 payload semantics: key is the whole string


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture
def api(monkeypatch, fresh_company):
    """A fake Polar API; records the requests it served."""
    monkeypatch.setenv("POLAR_ACCESS_TOKEN", "polar-test-token")
    monkeypatch.setattr(fresh_company, "polar_organization_id", "org-1")
    monkeypatch.setattr(fresh_company, "polar_base_url", "https://api.polar.sh/v1")
    calls: list[httpx.Request] = []
    state = {"orders": [fixture("order_paid")["data"]], "order": fixture("order_refunded_partial")["data"]}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer polar-test-token"
        path = request.url.path
        if path == "/v1/products/":
            assert request.url.params["organization_id"] == "org-1"
            return httpx.Response(200, json=fixture("products"))
        if path == "/v1/orders/":
            return httpx.Response(200, json={"items": state["orders"], "pagination": {"total_count": 1, "max_page": 1}})
        if path == f"/v1/orders/{ORDER_ID}":
            return httpx.Response(200, json=state["order"])
        if path == "/v1/refunds/" and request.method == "POST":
            return httpx.Response(state.get("refund_status", 201), json=fixture("refund_response"))
        return httpx.Response(404, json={"detail": "not found"})

    store = polar.PolarStore(transport=httpx.MockTransport(handler))
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
def test_sync_catalog_maps_products(api):
    assert api.sync_catalog() == {"products": 4, "created": 4, "updated": 0}
    by_sku = {p["sku"]: p for p in api.list_products()}
    lic = by_sku["PL-p-101"]
    assert (lic["title"], lic["kind"], lic["price_eur"], lic["status"]) == ("FocusFlow Pro", "license", 49.0, "active")
    assert by_sku["PL-p-201"]["kind"] == "download" and by_sku["PL-p-301"]["kind"] == "subscription"
    assert by_sku["PL-p-401"]["status"] == "draft"
    assert api.sync_catalog()["updated"] == 4
    assert all(i["available_keys"] is None for i in api.inventory())


def test_get_store_returns_polar(monkeypatch, fresh_company):
    monkeypatch.setattr(fresh_company, "store_adapter", "polar")
    assert isinstance(get_store(), polar.PolarStore)


def test_catalog_writes_are_refused(api):
    with pytest.raises(NotImplementedError, match="Polar dashboard"):
        api.update_price("PL-p-101", 5.0)
    with pytest.raises(NotImplementedError):
        api.create_order("a@b.c", "PL-p-101")


# --------------------------------------------------------------------------- mirroring
def test_mirror_order_books_polar_tax_delivered_and_emailed(api):
    api.sync_catalog()
    r = polar.mirror_order(fixture("order_paid"))
    assert r["external_id"] == ORDER_ID and r["status"] == "delivered"
    with session() as s:
        o = s.get(Order, r["order_id"])
        c = s.get(Customer, o.customer_id)
        assert (o.amount_eur, o.vat_eur, o.email_sent, o.campaign_id) == (49.0, 8.5, True, "c-1")
        assert s.get(Product, o.product_id).external_id == "p-101"
        assert (c.email, c.country, c.name) == ("eva@example.nl", "NL", "Eva de Vries")
        assert o.withdrawal_consent_version == "2026-10"
    got = lines(r["order_id"])
    assert got[0] == ("cash", 49.0, 0.0, None, None) and got[1][:3] == ("revenue", 0.0, 40.5)
    assert got[2][:4] == ("vat_payable", 0.0, 8.5, "NL") and round(got[2][4], 3) == 0.21
    drain()
    assert check_all() == []


def test_mirror_is_idempotent_and_ignores_unpaid(fresh_company):
    first = polar.mirror_order(fixture("order_paid"))  # product not synced: placeholder keeps the sale
    before = counts()
    assert polar.mirror_order(fixture("order_paid"))["already"] is True
    assert counts() == before
    pending = fixture("order_paid")
    pending["data"].update(id="other", status="pending")
    assert polar.mirror_order(pending)["status"] == "ignored"
    assert counts() == before and first["order_id"]


def test_foreign_currency_is_refused(fresh_company):
    usd = fixture("order_paid")
    usd["data"]["currency"] = "usd"
    with pytest.raises(ValueError, match="not converted"):
        polar.mirror_order(usd)


def test_partial_then_full_refund_book_only_the_difference(api):
    api.sync_catalog()
    polar.mirror_order(fixture("order_paid"))
    r = polar.mirror_refund(fixture("order_refunded_partial"))
    assert r["refunded_eur"] == 12.1 and r["status"] == "partially_refunded"
    assert polar.mirror_refund(fixture("order_refunded_partial"))["already"] is True  # replay
    r = polar.mirror_refund(fixture("order_refunded_full"))
    assert r["refunded_eur"] == 36.9 and r["status"] == "refunded" and r["remaining_eur"] == 0
    with session() as s:
        assert s.scalar(select(func.sum(Refund.amount_eur))) == pytest.approx(49.0)
    vat = sum(c - d for a, d, c, *_ in lines(r["order_id"]) if a == "vat_payable")
    assert round(vat, 2) == 0.0  # sale VAT fully reversed
    drain()
    assert check_all() == []


def test_refund_arriving_first_mirrors_the_order(fresh_company):
    r = polar.mirror_refund(fixture("order_refunded_full"))
    assert r["status"] == "refunded"
    assert counts()["Order"] == 1


def test_dispatch_ignores_other_events():
    assert polar.dispatch({"type": "subscription.created", "data": {}}) == {"event": "subscription.created",
                                                                           "status": "ignored"}


# --------------------------------------------------------------------------- API: reconcile and refunds
def test_reconcile_recent_mirrors_missing_orders_and_is_idempotent(api):
    api.sync_catalog()
    since = datetime(2026, 10, 1)
    r = api.reconcile_recent(since)
    assert (r["listed"], r["mirrored"], r["errors"]) == (1, 1, 0)
    again = api.reconcile_recent(since)
    assert (again["mirrored"], again["unchanged"]) == (0, 1)
    api.state["orders"] = [fixture("order_refunded_partial")["data"]]
    assert api.reconcile_recent(since)["refunds"] == 1
    assert api.reconcile_recent(since)["refunds"] == 0


def test_reconcile_ignores_orders_older_than_since(api):
    assert api.reconcile_recent(datetime(2026, 11, 1))["listed"] == 0


def test_refund_order_posts_net_amount_and_mirrors_state(api):
    api.sync_catalog()
    local = polar.mirror_order(fixture("order_paid"))["order_id"]
    r = api.refund_order(local, 12.10, "customer asked")
    posts = [c for c in api.calls if c.method == "POST"]
    sent = json.loads(posts[0].content)
    assert sent["order_id"] == ORDER_ID and sent["amount"] == 1000 and sent["reason"] == "customer_request"
    assert r["status"] == "partially_refunded" and r["amount_eur"] == 12.10


def test_refund_already_refunded_upstream_is_mirrored_not_raised(api):
    local = polar.mirror_order(fixture("order_paid"))["order_id"]
    api.state["refund_status"] = 403
    api.state["order"] = fixture("order_refunded_full")["data"]
    assert api.refund_order(local, 49.0, "dup")["status"] == "refunded"


def test_refund_validates_amount_and_order(api):
    local = polar.mirror_order(fixture("order_paid"))["order_id"]
    with pytest.raises(ValueError, match="exceeds"):
        api.refund_order(local, 99.0, "x")
    with pytest.raises(ValueError, match="not found"):
        api.refund_order("nope", 1.0, "x")


def test_reconciler_runs_polar_pass(api, fresh_company, monkeypatch):
    monkeypatch.setattr(fresh_company, "store_adapter", "polar")
    monkeypatch.setattr("aecom.store.adapter.get_store", lambda: api)
    api.state["orders"][0]["created_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # inside the reconciler's window
    r = reconcile()
    assert r["polar_listed"] == 1 and r["polar_mirrored"] == 1


# --------------------------------------------------------------------------- signature
def sign(raw: bytes, key: bytes, msg_id="msg_1", ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": f"v1,bogus v1,{sig}",
            "content-type": "application/json"}


def std_key(secret=SECRET) -> bytes:
    return base64.b64decode(secret.removeprefix("whsec_"))


def test_signature_accepts_standard_and_legacy_keys():
    raw = b'{"type":"x"}'
    h = sign(raw, std_key())
    assert polar.verify_signature(raw, h["webhook-id"], h["webhook-timestamp"], h["webhook-signature"], SECRET)
    h = sign(raw, LEGACY_SECRET.encode())
    assert polar.verify_signature(raw, h["webhook-id"], h["webhook-timestamp"], h["webhook-signature"], LEGACY_SECRET)


def test_signature_rejects_tampering_stale_and_missing():
    raw = b'{"type":"x"}'
    h = sign(raw, std_key())
    args = (h["webhook-id"], h["webhook-timestamp"], h["webhook-signature"])
    assert not polar.verify_signature(raw + b" ", *args, SECRET)
    assert not polar.verify_signature(raw, *args, "whsec_" + base64.b64encode(b"other").decode())
    assert not polar.verify_signature(raw, None, *args[1:], SECRET)
    assert not polar.verify_signature(raw, *args, "")
    old = sign(raw, std_key(), ts=int(time.time()) - 6 * 60)
    assert not polar.verify_signature(raw, old["webhook-id"], old["webhook-timestamp"], old["webhook-signature"], SECRET)


# --------------------------------------------------------------------------- route
@pytest.fixture
def client(monkeypatch, fresh_company):
    configured = {"POLAR_WEBHOOK_SECRET": SECRET}
    monkeypatch.setattr(webhooks.secrets, "get", lambda name: configured.get(name))
    c = TestClient(api_app.app)
    c.configured = configured
    return c


def post(client, payload: dict, **kw):
    raw = json.dumps(payload).encode()
    return client.post("/webhooks/polar", content=raw, headers=sign(raw, std_key(), **kw))


def test_route_mirrors_once_and_dedupes_on_webhook_id(client):
    r = post(client, fixture("order_paid"))
    assert r.status_code == 200 and r.json()["duplicate"] is False and r.json()["event"] == "order.paid"
    r = post(client, fixture("order_paid"))
    assert r.json()["duplicate"] is True
    assert counts()["Order"] == 1
    with session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookReceipt)) == 1


def test_route_refund_and_ignored_event(client):
    post(client, fixture("order_paid"))
    r = post(client, fixture("order_refunded_full"), msg_id="msg_2")
    assert r.status_code == 200 and r.json()["status"] == "refunded"
    r = post(client, {"type": "subscription.created", "data": {}}, msg_id="msg_3")
    assert r.status_code == 200 and r.json()["status"] == "ignored"


def test_route_rejects_bad_signature_and_missing_secret(client):
    raw = json.dumps(fixture("order_paid")).encode()
    assert client.post("/webhooks/polar", content=raw).status_code == 401
    assert client.post("/webhooks/polar", content=raw, headers=sign(raw, b"wrong")).status_code == 401
    assert counts()["Order"] == 0
    client.configured.clear()
    assert client.post("/webhooks/polar", content=raw, headers=sign(raw, std_key())).status_code == 503


def test_route_failed_mirror_is_500_and_retry_succeeds(client, monkeypatch):
    real = polar.EVENT_HANDLERS["order.paid"]
    monkeypatch.setitem(polar.EVENT_HANDLERS, "order.paid", lambda payload, session=None: 1 / 0)
    assert post(client, fixture("order_paid")).status_code == 500
    with session() as s:
        assert s.scalar(select(func.count()).select_from(WebhookReceipt)) == 0
        assert s.scalars(select(ErrorRow.component)).all() == ["webhook.polar"]
    monkeypatch.setitem(polar.EVENT_HANDLERS, "order.paid", real)
    assert post(client, fixture("order_paid")).json()["duplicate"] is False
    assert counts()["Order"] == 1
