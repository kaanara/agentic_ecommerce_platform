"""Polar storefront (STORE_ADAPTER=polar), an alternative to Lemon Squeezy (Roadmap L1). Polar is the system of record.

Polar (https://polar.sh) is a merchant of record like Lemon Squeezy: it runs checkout and payment, collects and files
VAT/sales tax, delivers benefits (licence keys, files) and emails receipts. This module **mirrors** its data into the
local tables and ledger exactly like ``aecom.store.lemonsqueezy`` does, and reuses that module's mirror helpers:

* ``sync_catalog()`` reads ``GET /v1/products/`` and upserts one local ``Product`` per Polar product
  (``Product.external_id`` = Polar product id, SKU ``PL-<id>``).
* ``mirror_order`` / ``mirror_refund`` are pure functions on webhook payloads (``{"type", "data": Order}``), idempotent
  on the Polar order id, and take the webhook route's ``session=`` so receipt and mirror commit together.
* ``order.paid`` books the sale; ``order.refunded`` books the part of the cumulative refund not mirrored yet. Other
  events are acknowledged and ignored. ``reconcile_recent`` repairs lost webhooks from ``GET /v1/orders/``.
* Mirrored orders are already ``delivered`` with ``email_sent`` set (Polar delivers and sends the receipt) and
  publish ``order.mirrored``, never ``order.paid``.

Products and prices are managed in the Polar dashboard; the write methods raise ``NotImplementedError``. Licence
keys are issued by Polar's benefits and are not mirrored (v1). Refunds use ``POST /v1/refunds/``.

Amounts: Polar sends integer cents (``net_amount`` before tax, ``tax_amount``, ``total_amount`` = gross). The ledger
books the gross and the VAT exactly as Polar charged it. The refunded gross is ``refunded_amount +
refunded_tax_amount``. The refund request sends the net part of the gross (Polar adds the proportional tax); verify
that against the sandbox before the first live refund.

API: https://polar.sh/docs/api-reference. Webhooks: https://polar.sh/docs/integrate/webhooks/delivery
(Standard Webhooks headers; secrets created before 2026-09-08 use the legacy key, the whole ``whsec_`` string as
UTF-8 bytes, newer ones the base64 part after ``whsec_``. Both are tried).
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aecom import ledger
from aecom.bus.events import publish
from aecom.config import get_settings
from aecom.db import Customer, LicenseKey, Order, Product, Refund, now, session
from aecom.money import cents
from aecom.secrets import secrets
from aecom.store import standard_webhooks
from aecom.store.lemonsqueezy import _eur, _mirrored, _o, _opted_in, _post_refund, _refunded_c, _ts, _tx
from aecom.store.mock import MockStore

PROVIDER = "polar"
MANAGED_IN_DASHBOARD = ("Products and prices are managed in the Polar dashboard (system of record). "
                        "Ask a human to change it there, then run sync_catalog().")
# our checkout passes these as checkout/order metadata (same keys as the Lemon Squeezy custom data)
CUSTOM_COUNTRY = "country"
CUSTOM_CONSENT_AT = "withdrawal_consent_at"
CUSTOM_CONSENT_VERSION = "withdrawal_consent_version"
CUSTOM_CAMPAIGN = "campaign_id"
CUSTOM_MARKETING_CONSENT = "marketing_consent"
MIRRORABLE = ("paid", "refunded", "partially_refunded")


def _now() -> float:
    return time.time()


# --------------------------------------------------------------------------- signature
def verify_signature(raw_body: bytes, msg_id: str | None, timestamp: str | None, signature: str | None,
                     secret: str | None = None) -> bool:
    """Standard Webhooks check (``aecom.store.standard_webhooks``) under ``POLAR_WEBHOOK_SECRET``, both key forms.
    No secret configured -> reject."""
    secret = secret if secret is not None else secrets.get("POLAR_WEBHOOK_SECRET")
    return standard_webhooks.verify(raw_body, msg_id, timestamp, signature, secret, now=_now())


# --------------------------------------------------------------------------- helpers
def _order(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    data = payload.get("data") or {}
    if not data.get("id"):
        raise ValueError("Polar payload has no order in 'data'")
    return str(data["id"]), data


def _country(a: dict[str, Any], override: str | None) -> str | None:
    custom = a.get("metadata") or {}
    customer = a.get("customer") or {}
    raw = (override or (a.get("billing_address") or {}).get("country")
           or (customer.get("billing_address") or {}).get("country") or custom.get(CUSTOM_COUNTRY) or "")
    return str(raw).upper() or None


def _gross_refunded_c(a: dict[str, Any]) -> int:
    return int(a.get("refunded_amount") or 0) + int(a.get("refunded_tax_amount") or 0)


# --------------------------------------------------------------------------- mirroring (webhook payloads)
def _create_mirror(s: Session, ext: str, a: dict[str, Any], country: str | None) -> Order:
    """Customer, order and sale ledger lines for a Polar order, inside the caller's transaction."""
    currency = str(a.get("currency") or "").upper()
    if currency != get_settings().polar_currency.upper():
        raise ValueError(f"Polar order {ext} is in {currency or '?'}, the ledger books "
                         f"{get_settings().polar_currency}; not converted, mirror it manually")
    custom = a.get("metadata") or {}
    gross, vat = _eur(a.get("total_amount")), _eur(a.get("tax_amount"))
    net_c = int(a.get("net_amount") or 0)
    rate = float(Decimal(int(a.get("tax_amount") or 0)) / net_c) if net_c and a.get("tax_amount") else 0.0
    tax_country = _country(a, country)
    product_id = str(a.get("product_id") or "")
    p = s.scalars(select(Product).where(Product.external_id == product_id)).first() if product_id else None
    if p is None:  # sold before the catalog sync saw it: keep the sale, the next sync_catalog fills in the details
        p = Product(sku=f"PL-{product_id or ext}", title=(a.get("product") or {}).get("name") or f"Polar {product_id}",
                    price_eur=float(_eur(a.get("subtotal_amount") or a.get("total_amount"))), kind="download",
                    external_id=product_id or None, status="active")
        s.add(p)
        s.flush()
    customer = a.get("customer") or {}
    email = customer.get("email") or ""
    c = s.scalars(select(Customer).where(Customer.email == email)).first()
    campaign = custom.get(CUSTOM_CAMPAIGN)
    if c is None:
        c = Customer(email=email, name=customer.get("name") or a.get("billing_name") or "",
                     country=tax_country or "", acquisition_channel=campaign)
        s.add(c)
        s.flush()
    elif tax_country and not c.country:
        c.country = tax_country
    if _opted_in(custom.get(CUSTOM_MARKETING_CONSENT)) and not c.marketing_consent:
        c.marketing_consent, c.marketing_consent_source = True, "checkout"
        c.marketing_consent_at = _ts(a.get("created_at")) or now()
    o = Order(customer_id=c.id, product_id=p.id, quantity=1, amount_eur=float(gross), vat_eur=float(vat),
              campaign_id=campaign, status="delivered", email_sent=True, email_sent_at=now(), external_id=ext,
              withdrawal_consent_at=_ts(custom.get(CUSTOM_CONSENT_AT)),
              withdrawal_consent_version=custom.get(CUSTOM_CONSENT_VERSION) if custom.get(CUSTOM_CONSENT_AT) else None,
              created_at=_ts(a.get("created_at")) or now())
    s.add(o)
    s.flush()
    # VAT exactly as Polar charged it (merchant of record), never recomputed from our rate table
    ledger.record_sale(o.id, float(gross), float(vat), p.unit_cost_eur, country=tax_country, vat_rate=rate, session=s)
    publish("order.mirrored", {"order_id": o.id, "external_id": ext, "sku": p.sku, "email": email,
                               "amount_eur": o.amount_eur, "vat_eur": o.vat_eur}, source=PROVIDER, session=s)
    return o


def mirror_order(payload: dict[str, Any], country: str | None = None, *, session: Session | None = None) -> dict[str, Any]:
    """Mirror an ``order.paid`` payload. Idempotent on the Polar order id. Orders that are not ``paid`` (pending,
    draft, void) are ignored; refund states go through ``mirror_refund``."""
    ext, a = _order(payload)
    if a.get("status") != "paid":
        return {"external_id": ext, "status": "ignored", "reason": f"order status {a.get('status')}"}
    with _tx(session) as s:
        existing = _mirrored(s, ext)
        if existing is not None:
            return {**_o(existing), "already": True}
        return _o(_create_mirror(s, ext, a, country))


def mirror_refund(payload: dict[str, Any], country: str | None = None,
                  reason: str = "refunded in Polar", *, session: Session | None = None) -> dict[str, Any]:
    """Mirror an ``order.refunded`` payload (full or partial). Polar's cumulative refunded gross is authoritative:
    only the part not mirrored yet is booked, so replays and out-of-order deliveries change nothing. An order seen
    for the first time here is mirrored first."""
    ext, a = _order(payload)
    with _tx(session) as s:
        o = _mirrored(s, ext) or _create_mirror(s, ext, a, country)
        total_c = cents(o.amount_eur)
        target_c = min(_gross_refunded_c(a), total_c)
        if a.get("status") == "refunded" and not target_c:
            target_c = total_c
        delta_c = target_c - _refunded_c(s, o.id)
        if delta_c <= 0:
            return {**_o(o), "already": True}
        s.add(Refund(order_id=o.id, amount_eur=delta_c / 100, reason=reason))
        full = target_c >= total_c
        o.status = "refunded" if full else "partially_refunded"
        if full:
            for key in s.scalars(select(LicenseKey).where(LicenseKey.order_id == o.id)):
                key.status = "revoked"
        _post_refund(s, o, delta_c, target_c)
        publish("order.refunded", {"order_id": o.id, "external_id": ext, "amount_eur": delta_c / 100,
                                   "status": o.status}, source=PROVIDER, session=s)
        return {**_o(o), "refunded_eur": delta_c / 100, "remaining_eur": (total_c - target_c) / 100}


EVENT_HANDLERS = {
    "order.paid": mirror_order,
    "order.refunded": mirror_refund,
}


def dispatch(payload: dict[str, Any], *, session: Session | None = None) -> dict[str, Any]:
    """Route a verified webhook body by its ``type``. Events we do not mirror are acknowledged and ignored."""
    event = str(payload.get("type") or "")
    handler = EVENT_HANDLERS.get(event)
    if handler is None:
        return {"event": event, "status": "ignored"}
    return {"event": event, **handler(payload, session=session)}


# --------------------------------------------------------------------------- adapter
class PolarStore:
    """``StoreAdapter`` backed by Polar. Reads come from the local mirror (kept current by webhooks and
    ``sync_catalog``). Catalog writes are refused, because the Polar dashboard owns the catalog."""

    name = PROVIDER

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = get_settings()
        self._transport = transport  # tests inject httpx.MockTransport; production uses the network
        self._local = MockStore()

    # ---- REST API
    def _client(self) -> httpx.Client:
        token = secrets.get("POLAR_ACCESS_TOKEN")
        if not token:
            raise RuntimeError("POLAR_ACCESS_TOKEN is not set. A human provisions it (request_provisioning) "
                               "and puts it in .env.")
        return httpx.Client(base_url=self.settings.polar_base_url, timeout=self.settings.polar_timeout_s,
                            transport=self._transport,
                            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})

    def _org_id(self) -> str:
        oid = self.settings.polar_organization_id
        if not oid:
            raise RuntimeError("POLAR_ORGANIZATION_ID is not set")
        return str(oid)

    @staticmethod
    def _get_pages(c: httpx.Client, path: str, params: dict[str, Any], stop_before: datetime | None = None) -> list[dict[str, Any]]:
        """All pages of a list endpoint (``page`` until ``pagination.max_page``). With ``stop_before`` (newest first
        lists) paging ends at the first page that reaches back past it, and older rows are dropped."""
        out: list[dict[str, Any]] = []
        number = 1
        while True:
            r = c.get(path, params={**params, "page": number, "limit": 100})
            r.raise_for_status()
            body = r.json()
            page = body.get("items") or []
            if stop_before is not None:
                fresh = [o for o in page if (_ts(o.get("created_at")) or stop_before) >= stop_before]
                out.extend(fresh)
                if len(fresh) < len(page):
                    return out
            else:
                out.extend(page)
            if not page or number >= ((body.get("pagination") or {}).get("max_page") or 1):
                return out
            number += 1

    def sync_catalog(self) -> dict[str, Any]:
        """Upsert one local product per Polar product (``external_id`` = product id).

        New products get the SKU ``PL-<id>``; existing rows keep their SKU, unit cost and vendor. A product is
        ``active`` when it is public and not archived. Kind: ``license`` if a licence-key benefit is attached,
        ``subscription`` if recurring, else ``download``. The price is the first fixed price (pay-what-you-want and
        metered prices book 0 here: the real amount is on each order).
        """
        with self._client() as c:
            products = self._get_pages(c, "/products/", {"organization_id": self._org_id(), "is_archived": "false"})
        created = updated = 0
        with session() as s:
            for prod in products:
                benefits = {b.get("type") for b in prod.get("benefits") or []}
                price = next((p for p in prod.get("prices") or []
                              if p.get("amount_type") == "fixed" and not p.get("is_archived")), None)
                row = s.scalars(select(Product).where(Product.external_id == str(prod["id"]))).first()
                if "license_keys" in benefits:
                    kind = "license"
                elif prod.get("is_recurring"):
                    kind = "subscription"
                else:
                    kind = row.kind if row is not None and row.kind not in ("license", "subscription") else "download"
                sellable = not prod.get("is_archived") and prod.get("visibility", "public") == "public"
                fields = {"title": prod.get("name") or "", "price_eur": float(_eur(price.get("price_amount") if price else 0)),
                          "kind": kind, "description": prod.get("description") or "",
                          "status": "active" if sellable else "draft"}
                if row is None:
                    s.add(Product(sku=f"PL-{prod['id']}", external_id=str(prod["id"]), **fields))
                    created += 1
                else:
                    for k, val in fields.items():
                        setattr(row, k, val)
                    updated += 1
        return {"products": len(products), "created": created, "updated": updated}

    # ---- lost-webhook recovery
    def recent_orders(self, since: datetime) -> list[dict[str, Any]]:
        """Orders created at or after ``since`` (naive UTC), newest first, from ``GET /v1/orders/``."""
        with self._client() as c:
            return self._get_pages(c, "/orders/", {"organization_id": self._org_id(), "sorting": "-created_at"},
                                   stop_before=since)

    def reconcile_recent(self, since: datetime) -> dict[str, int]:
        """Repair lost or failed webhooks: mirror every recent Polar order that is missing locally, and book the
        rest of any refund whose local total is below Polar's refunded gross. Idempotent. A failing order is
        recorded and counted in ``errors``; the others go on. (Polar's order carries the billing country.)"""
        from aecom.observability import record_error

        orders = self.recent_orders(since)
        counts = {"listed": len(orders), "mirrored": 0, "refunds": 0, "unchanged": 0, "ignored": 0, "errors": 0}
        for data in orders:
            ext = str(data.get("id"))
            try:
                with session() as s:
                    o = _mirrored(s, ext)
                    local_c = _refunded_c(s, o.id) if o is not None else 0
                    total_c = cents(o.amount_eur) if o is not None else int(data.get("total_amount") or 0)
                provider_c = min(_gross_refunded_c(data), total_c)
                if o is None and data.get("status") not in MIRRORABLE:
                    counts["ignored"] += 1  # draft / pending / void: mirror_order would ignore it as well
                    continue
                payload = {"type": "order.paid", "data": data}
                if o is None:
                    if provider_c:  # mirror_refund mirrors the order first
                        mirror_refund(payload)
                        counts["refunds"] += 1
                    else:
                        mirror_order(payload)
                    counts["mirrored"] += 1
                elif provider_c > local_c:
                    mirror_refund({"type": "order.refunded", "data": data})
                    counts["refunds"] += 1
                else:
                    counts["unchanged"] += 1
            except Exception as exc:
                counts["errors"] += 1
                record_error("polar.reconcile", exc, context={"external_id": ext})
        return counts

    # ---- local mirror reads
    def list_products(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._local.list_products(status)

    def get_product(self, sku: str) -> dict[str, Any] | None:
        return self._local.get_product(sku)

    def inventory(self) -> list[dict[str, Any]]:
        """Polar issues licence keys per purchase (benefit), so there is no local key stock to run out of."""
        return [{**item, "available_keys": None} for item in self._local.inventory()]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self._local.get_order(order_id)
        if order is not None:
            with session() as s:
                order["external_id"] = s.get(Order, order_id).external_id
        return order

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._local.list_orders(limit)

    # ---- managed in the Polar dashboard
    def create_product(self, sku, title, price_eur, kind, description="", unit_cost_eur=0.0, vendor=None, asset_url=None):
        raise NotImplementedError(f"create_product: {MANAGED_IN_DASHBOARD}")

    def update_price(self, sku: str, price_eur: float) -> dict[str, Any]:
        raise NotImplementedError(f"update_price: {MANAGED_IN_DASHBOARD}")

    def set_status(self, sku: str, status: str) -> dict[str, Any]:
        raise NotImplementedError(f"set_status: {MANAGED_IN_DASHBOARD}")

    def add_license_keys(self, sku: str, keys: list[str]) -> int:
        raise NotImplementedError("add_license_keys: Polar generates licence keys per purchase "
                                  "(attach a 'license keys' benefit to the product in the dashboard)")

    def create_order(self, customer_email: str, sku: str, country: str = "DE", campaign_id: str | None = None):
        raise NotImplementedError("create_order: orders are placed through Polar checkout and mirrored "
                                  "from its order.paid webhook")

    # ---- fulfilment: done by Polar
    def deliver_order(self, order_id: str) -> dict[str, Any]:
        """No-op. Polar has already delivered the benefits and emailed the customer."""
        return {"order_id": order_id, "status": "delivered", "already": True, "delivered_by": PROVIDER}

    def verify_download(self, order_id: str, token: str) -> bool:
        return self._local.verify_download(order_id, token)

    def mark_email_sent(self, order_id: str) -> None:
        self._local.mark_email_sent(order_id)

    def refund_order(self, order_id: str, amount_eur: float, reason: str) -> dict[str, Any]:
        """Refund through the API (``POST /v1/refunds/``; ``amount`` is the net part, Polar adds the tax), then
        re-read the order and mirror it. The later ``order.refunded`` webhook is a no-op (idempotent mirror)."""
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            if not o.external_id:
                raise ValueError(f"order {order_id} is not a Polar order")
            ext, status, total_c, vat_c = o.external_id, o.status, cents(o.amount_eur), cents(o.vat_eur)
            remaining = total_c - cents(s.scalar(select(func.coalesce(func.sum(Refund.amount_eur), 0.0))
                                                 .where(Refund.order_id == order_id)))
        if status == "refunded":
            return {"order_id": order_id, "status": "refunded", "already": True}
        if not 0 < cents(amount_eur) <= remaining:
            raise ValueError(f"refund exceeds remaining amount: requested {amount_eur:.2f}, "
                             f"remaining {remaining / 100:.2f}")
        net_c = max(round(cents(amount_eur) * (total_c - vat_c) / total_c), 1) if total_c else cents(amount_eur)
        body = {"order_id": ext, "reason": "customer_request", "amount": net_c, "comment": reason[:500]}
        with self._client() as c:
            r = c.post("/refunds/", json=body)
            if r.status_code != 403:  # 403 RefundedAlready: nothing to send, just mirror Polar's state
                r.raise_for_status()
            g = c.get(f"/orders/{ext}")
            g.raise_for_status()
            data = g.json()
        result = mirror_refund({"type": "order.refunded", "data": data}, reason=reason)
        return {**result, "amount_eur": amount_eur}
