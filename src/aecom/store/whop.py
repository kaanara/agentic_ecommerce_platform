"""Whop storefront (STORE_ADAPTER=whop). Whop is the system of record for payments; **agents manage the catalog**.

Unlike Lemon Squeezy and Polar, where the dashboard owns the catalog, the Whop adapter writes products, variants
(prices) and visibility through the REST API (https://docs.whop.com/api-reference/beta/overview), every write gated
by ``policy.yaml`` like any other tool. Broader shop settings (promo codes, checkout links, account preferences) go
through the allowlisted ``whop_request`` operations in ``aecom.store.whop_ops``.

Payments are **mirrored** into the local tables and ledger the same way ``aecom.store.lemonsqueezy`` does it, reusing
its helpers:

* ``mirror_payment`` handles a paid Payment (``payment.succeeded``): customer, order (``external_id`` = ``pay_`` id),
  sale ledger lines with Whop's ``total`` as gross and ``tax_amount`` as VAT. Publishes ``order.mirrored``.
* ``mirror_refund`` books the part of the payment's cumulative ``refunded_amount`` not mirrored yet. The webhook route
  gets ``refund.created`` / ``refund.updated`` (Whop has no ``payment.refunded``), fetches the payment first and
  passes it here, so no network call runs inside the database transaction.
* ``reconcile_recent`` repairs lost webhooks from ``GET /payments``.

Money: Whop sends money objects ``{"amount": "49.00", "currency": "eur", ...}`` in major units as decimal strings
(``Decimal``, never float). Prices on variants are plain numbers in the variant currency. Every POST/PATCH/DELETE
carries an ``Idempotency-Key`` derived from the request, so a retried agent call never creates a second object.

Open points (doc-derived, check in the sandbox): whether Whop is the merchant of record for EU VAT, and whether
``refunded_amount`` includes the refunded tax (assumed: yes, it is the gross refunded).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aecom import ledger
from aecom.bus.events import publish
from aecom.config import get_settings
from aecom.db import Customer, LicenseKey, Order, Product, Refund, now, session
from aecom.money import cents, quantize
from aecom.secrets import secrets
from aecom.store import standard_webhooks
from aecom.store.lemonsqueezy import _mirrored, _o, _opted_in, _post_refund, _refunded_c, _ts, _tx
from aecom.store.mock import MockStore

PROVIDER = "whop"
CUSTOM_COUNTRY = "country"
CUSTOM_CONSENT_AT = "withdrawal_consent_at"
CUSTOM_CONSENT_VERSION = "withdrawal_consent_version"
CUSTOM_CAMPAIGN = "campaign_id"
CUSTOM_MARKETING_CONSENT = "marketing_consent"
REFUND_EVENTS = ("refund.created", "refund.updated")
# local Product.status -> Whop visibility
VISIBILITY = {"active": "visible", "draft": "hidden", "archived": "archived"}


class WhopError(RuntimeError):
    """A Whop API error, with Whop's own message (agents see it as the tool result)."""


def _now() -> float:
    return time.time()


def verify_signature(raw_body: bytes, msg_id: str | None, timestamp: str | None, signature: str | None,
                     secret: str | None = None) -> bool:
    """Standard Webhooks check (``aecom.store.standard_webhooks``) under ``WHOP_WEBHOOK_SECRET`` (``ws_...``).
    No secret configured -> reject."""
    secret = secret if secret is not None else secrets.get("WHOP_WEBHOOK_SECRET")
    return standard_webhooks.verify(raw_body, msg_id, timestamp, signature, secret, now=_now())


# --------------------------------------------------------------------------- helpers
def _money(m: Any) -> Decimal:
    """A Whop money object (or bare number / string) as a quantized Decimal; None -> 0."""
    if m is None:
        return Decimal(0)
    raw = m.get("amount") if isinstance(m, dict) else m
    try:
        return quantize(Decimal(str(raw or 0)))
    except InvalidOperation:
        return Decimal(0)


def _currency(a: dict[str, Any]) -> str:
    total = a.get("total")
    return str(a.get("currency") or (total.get("currency") if isinstance(total, dict) else "") or "").upper()


def _payment(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    data = payload.get("data") or {}
    if not data.get("id"):
        raise ValueError("Whop payload has no payment in 'data'")
    return str(data["id"]), data


def _refunded_gross_c(a: dict[str, Any]) -> int:
    return cents(_money(a.get("refunded_amount")))


# --------------------------------------------------------------------------- mirroring
def _create_mirror(s: Session, ext: str, a: dict[str, Any], country: str | None) -> Order:
    """Customer, order and sale ledger lines for a Whop payment, inside the caller's transaction."""
    currency = _currency(a)
    if currency != get_settings().whop_currency.upper():
        raise ValueError(f"Whop payment {ext} is in {currency or '?'}, the ledger books "
                         f"{get_settings().whop_currency}; not converted, mirror it manually")
    custom = a.get("metadata") or {}
    gross, vat = _money(a.get("total")), _money(a.get("tax_amount"))
    net = gross - vat
    rate = float(vat / net) if net and vat else 0.0
    tax_country = str(country or (a.get("billing_address") or {}).get("country") or custom.get(CUSTOM_COUNTRY)
                      or "").upper() or None
    items = a.get("line_items") or []
    variant_id = str(a.get("variant_id") or (items[0].get("variant_id") if items else "") or "")
    p = s.scalars(select(Product).where(Product.external_id == variant_id)).first() if variant_id else None
    if p is None:  # sold before the catalog sync saw it: keep the sale, the next sync_catalog fills in the details
        p = Product(sku=f"WH-{variant_id or ext}", title=f"Whop {variant_id or ext}", price_eur=float(gross),
                    kind="download", external_id=variant_id or None, status="active")
        s.add(p)
        s.flush()
    email = a.get("customer_email") or ""
    c = s.scalars(select(Customer).where(Customer.email == email)).first()
    campaign = custom.get(CUSTOM_CAMPAIGN)
    if c is None:
        name = (a.get("user") or {}).get("name") or (a.get("billing_address") or {}).get("name") or ""
        c = Customer(email=email, name=name, country=tax_country or "", acquisition_channel=campaign)
        s.add(c)
        s.flush()
    elif tax_country and not c.country:
        c.country = tax_country
    if _opted_in(custom.get(CUSTOM_MARKETING_CONSENT)) and not c.marketing_consent:
        c.marketing_consent, c.marketing_consent_source = True, "checkout"
        c.marketing_consent_at = _ts(a.get("paid_at") or a.get("created_at")) or now()
    quantity = max(int((items[0].get("quantity") if items else None) or 1), 1)
    o = Order(customer_id=c.id, product_id=p.id, quantity=quantity, amount_eur=float(gross), vat_eur=float(vat),
              campaign_id=campaign, status="delivered", email_sent=True, email_sent_at=now(), external_id=ext,
              withdrawal_consent_at=_ts(custom.get(CUSTOM_CONSENT_AT)),
              withdrawal_consent_version=custom.get(CUSTOM_CONSENT_VERSION) if custom.get(CUSTOM_CONSENT_AT) else None,
              created_at=_ts(a.get("paid_at") or a.get("created_at")) or now())
    s.add(o)
    s.flush()
    # VAT exactly as Whop charged it, never recomputed from our rate table
    ledger.record_sale(o.id, float(gross), float(vat), p.unit_cost_eur, country=tax_country, vat_rate=rate, session=s)
    publish("order.mirrored", {"order_id": o.id, "external_id": ext, "sku": p.sku, "email": email,
                               "amount_eur": o.amount_eur, "vat_eur": o.vat_eur}, source=PROVIDER, session=s)
    return o


def mirror_payment(payload: dict[str, Any], country: str | None = None, *,
                   session: Session | None = None) -> dict[str, Any]:
    """Mirror a paid Whop payment (``payment.succeeded``). Idempotent on the ``pay_`` id. Payments that are not
    ``paid`` (open, pending, void, ...) are ignored; refunds go through ``mirror_refund``."""
    ext, a = _payment(payload)
    if a.get("status") != "paid":
        return {"external_id": ext, "status": "ignored", "reason": f"payment status {a.get('status')}"}
    with _tx(session) as s:
        existing = _mirrored(s, ext)
        if existing is not None:
            return {**_o(existing), "already": True}
        return _o(_create_mirror(s, ext, a, country))


def mirror_refund(payload: dict[str, Any], country: str | None = None, reason: str = "refunded in Whop", *,
                  session: Session | None = None) -> dict[str, Any]:
    """Mirror a payment's refunds (``data`` = the Payment, as fetched after a ``refund.*`` event or returned by
    ``POST /payments/{id}/refund``). The cumulative ``refunded_amount`` is authoritative: only the part not mirrored
    yet is booked, so replays and out-of-order events change nothing. An unknown payment is mirrored first."""
    ext, a = _payment(payload)
    with _tx(session) as s:
        o = _mirrored(s, ext) or _create_mirror(s, ext, a, country)
        total_c = cents(o.amount_eur)
        target_c = min(_refunded_gross_c(a), total_c)
        if a.get("substatus") == "refunded" and not target_c:
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


def refund_payment_id(payload: dict[str, Any]) -> str | None:
    """For a ``refund.*`` event whose refund succeeded: the payment to fetch before mirroring; else None."""
    if payload.get("type") not in REFUND_EVENTS:
        return None
    data = payload.get("data") or {}
    if data.get("status") != "succeeded":
        return None
    return str((data.get("payment") or {}).get("id") or data.get("payment_id") or "") or None


def dispatch(payload: dict[str, Any], *, payment: dict[str, Any] | None = None,
             session: Session | None = None) -> dict[str, Any]:
    """Route a verified webhook body by ``type``. ``payment`` is the Payment the route fetched for a refund event
    (see ``refund_payment_id``). Events we do not mirror are acknowledged and ignored."""
    event = str(payload.get("type") or "")
    if event == "payment.succeeded":
        return {"event": event, **mirror_payment(payload, session=session)}
    if event in REFUND_EVENTS:
        if payment is None:
            return {"event": event, "status": "ignored", "reason": f"refund {(payload.get('data') or {}).get('status')}"}
        return {"event": event, **mirror_refund({"data": payment}, session=session)}
    return {"event": event, "status": "ignored"}


# --------------------------------------------------------------------------- adapter
class WhopStore:
    """``StoreAdapter`` backed by Whop. Reads come from the local mirror; catalog writes go to Whop first and are
    then mirrored locally (``external_id`` = variant id, ``plan_...``)."""

    name = PROVIDER

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = get_settings()
        self._transport = transport  # tests inject httpx.MockTransport; production uses the network
        self._local = MockStore()

    # ---- REST API
    def _client(self) -> httpx.Client:
        key = secrets.get("WHOP_API_KEY")
        if not key:
            raise RuntimeError("WHOP_API_KEY is not set. A human provisions it (request_provisioning) "
                               "and puts it in .env.")
        return httpx.Client(base_url=self.settings.whop_base_url, timeout=self.settings.whop_timeout_s,
                            transport=self._transport,
                            headers={"Accept": "application/json", "Authorization": f"Bearer {key}",
                                     "Api-Version-Date": self.settings.whop_api_version})

    def account_id(self) -> str:
        aid = self.settings.whop_account_id
        if not aid:
            raise RuntimeError("WHOP_ACCOUNT_ID is not set (the biz_... id of the store)")
        return str(aid)

    @staticmethod
    def idempotency_key(method: str, path: str, body: Any) -> str:
        raw = f"{method.upper()} {path} " + json.dumps(body, sort_keys=True, default=str)
        return "aecom-" + hashlib.sha256(raw.encode()).hexdigest()[:48]

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None,
                ok_statuses: tuple[int, ...] = ()) -> Any:
        """One API call. Writes carry an Idempotency-Key from the request itself. Errors raise ``WhopError`` with
        Whop's message; statuses in ``ok_statuses`` are returned as ``{"status_code": n, ...body}`` instead."""
        method = method.upper()
        headers = {}
        if method in ("POST", "PATCH", "PUT", "DELETE"):
            headers["Idempotency-Key"] = self.idempotency_key(method, path, body)
        with self._client() as c:
            r = c.request(method, path, params=params, json=body if body is not None else None, headers=headers)
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text[:500]}
        if r.status_code in ok_statuses:
            return {"status_code": r.status_code, **(data if isinstance(data, dict) else {"data": data})}
        if r.status_code >= 400:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise WhopError(f"Whop {method} {path} answered HTTP {r.status_code}: {msg or r.reason_phrase}")
        return data

    def pages(self, path: str, params: dict[str, Any], stop_before: datetime | None = None,
              limit: int | None = None) -> list[dict[str, Any]]:
        """All items of a cursor list (``first`` / ``after`` until ``page_info.has_next_page`` is false). With
        ``stop_before`` (newest-first lists) paging ends at the first item older than it."""
        out: list[dict[str, Any]] = []
        after = None
        while True:
            q = {**params, "first": 100, **({"after": after} if after else {})}
            body = self.request("GET", path, params=q)
            page = body.get("data") or []
            for item in page:
                if stop_before is not None and (_ts(item.get("created_at")) or stop_before) < stop_before:
                    return out
                out.append(item)
                if limit is not None and len(out) >= limit:
                    return out
            info = body.get("page_info") or {}
            after = info.get("end_cursor")
            if not page or not info.get("has_next_page") or not after:
                return out

    # ---- catalog sync
    def _upsert_variant(self, s: Session, v: dict[str, Any], product: dict[str, Any], siblings: int) -> bool:
        """One local product per Whop variant. Returns True if a row was created."""
        renewal = v.get("billing_type") == "renewal"
        row = s.scalars(select(Product).where(Product.external_id == str(v["id"]))).first()
        kind = "subscription" if renewal else (row.kind if row is not None and row.kind != "subscription" else "download")
        vt, pt = v.get("title") or "", product.get("title") or ""
        title = pt if siblings <= 1 or not vt else f"{pt} - {vt}"
        price = v.get("renewal_price") if renewal else v.get("initial_price")
        visible = v.get("visibility") == "visible" and product.get("visibility", "visible") == "visible"
        archived = "archived" in (v.get("visibility"), product.get("visibility"))
        fields = {"title": title or f"Whop {v['id']}", "price_eur": float(_money(price)), "kind": kind,
                  "description": product.get("description") or v.get("description") or "",
                  "status": "active" if visible else ("archived" if archived else "draft")}
        if row is None:
            s.add(Product(sku=v.get("sku") or f"WH-{v['id']}", external_id=str(v["id"]), **fields))
            return True
        for k, val in fields.items():
            setattr(row, k, val)
        return False

    def sync_catalog(self) -> dict[str, Any]:
        """Upsert one local product per Whop variant (``external_id`` = variant id). New rows take the variant's
        ``sku`` or ``WH-<variant id>``; existing rows keep their SKU, unit cost and vendor."""
        aid = self.account_id()
        products = {p["id"]: p for p in self.pages("/products", {"account_id": aid})}
        variants = self.pages("/variants", {"account_id": aid})
        per_product: dict[str, int] = {}
        for v in variants:
            pid = (v.get("product") or {}).get("id")
            per_product[pid] = per_product.get(pid, 0) + 1
        created = updated = 0
        with session() as s:
            for v in variants:
                pid = (v.get("product") or {}).get("id")
                product = products.get(pid) or v.get("product") or {}
                if self._upsert_variant(s, v, product, per_product.get(pid, 1)):
                    created += 1
                else:
                    updated += 1
        return {"products": len(products), "variants": len(variants), "created": created, "updated": updated}

    def resync_variant(self, variant_id: str) -> None:
        """Refresh one variant (and its product) into the local mirror after a write."""
        v = self.request("GET", f"/variants/{variant_id}")
        pid = (v.get("product") or {}).get("id")
        product = self.request("GET", f"/products/{pid}") if pid else {}
        siblings = len(product.get("variants") or []) or 1
        with session() as s:
            self._upsert_variant(s, v, product, siblings)

    def resync_product(self, product_id: str) -> None:
        product = self.request("GET", f"/products/{product_id}")
        for v in self.pages("/variants", {"account_id": self.account_id(), "product_ids": [product_id]}):
            with session() as s:
                self._upsert_variant(s, v, product, len(product.get("variants") or []) or 1)

    # ---- lost-webhook recovery
    def recent_payments(self, since: datetime) -> list[dict[str, Any]]:
        return self.pages("/payments", {"account_id": self.account_id(), "created_after": since.isoformat() + "Z",
                                        "statuses": ["paid"], "order": "created_at", "direction": "desc"},
                          stop_before=since)

    def reconcile_recent(self, since: datetime) -> dict[str, int]:
        """Mirror every recent paid Whop payment missing locally, and book the rest of any refund whose local total
        is below Whop's ``refunded_amount``. Idempotent; a failing payment is recorded and counted in ``errors``."""
        from aecom.observability import record_error

        payments = self.recent_payments(since)
        counts = {"listed": len(payments), "mirrored": 0, "refunds": 0, "unchanged": 0, "ignored": 0, "errors": 0}
        for data in payments:
            ext = str(data.get("id"))
            try:
                with session() as s:
                    o = _mirrored(s, ext)
                    local_c = _refunded_c(s, o.id) if o is not None else 0
                    total_c = cents(o.amount_eur) if o is not None else cents(_money(data.get("total")))
                provider_c = min(_refunded_gross_c(data), total_c)
                if o is None and data.get("status") != "paid":
                    counts["ignored"] += 1
                    continue
                if o is None:
                    if provider_c:
                        mirror_refund({"data": data})
                        counts["refunds"] += 1
                    else:
                        mirror_payment({"data": data})
                    counts["mirrored"] += 1
                elif provider_c > local_c:
                    mirror_refund({"data": data})
                    counts["refunds"] += 1
                else:
                    counts["unchanged"] += 1
            except Exception as exc:
                counts["errors"] += 1
                record_error("whop.reconcile", exc, context={"external_id": ext})
        return counts

    # ---- local mirror reads
    def list_products(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._local.list_products(status)

    def get_product(self, sku: str) -> dict[str, Any] | None:
        return self._local.get_product(sku)

    def inventory(self) -> list[dict[str, Any]]:
        """Whop memberships carry their own access (and licence keys), so there is no local key stock."""
        return [{**item, "available_keys": None} for item in self._local.inventory()]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self._local.get_order(order_id)
        if order is not None:
            with session() as s:
                order["external_id"] = s.get(Order, order_id).external_id
        return order

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._local.list_orders(limit)

    # ---- catalog writes (agents own the Whop catalog)
    def _variant_of(self, sku: str) -> tuple[str, dict[str, Any]]:
        p = self._local.get_product(sku)
        if p is None:
            raise ValueError(f"unknown SKU {sku}")
        with session() as s:
            ext = s.scalars(select(Product.external_id).where(Product.sku == sku)).one()
        if not ext:
            raise ValueError(f"SKU {sku} is not a Whop product (no variant id); run sync_catalog()")
        return ext, p

    def create_product(self, sku, title, price_eur, kind, description="", unit_cost_eur=0.0, vendor=None, asset_url=None):
        """Create a hidden Whop product with one hidden variant (one-time, or monthly for a subscription), then the
        local draft row. Publishing is a separate step (``set_status(sku, "active")``)."""
        if self._local.get_product(sku) is not None:
            raise ValueError(f"SKU {sku} already exists")
        aid = self.account_id()
        product = self.request("POST", "/products", body={"account_id": aid, "title": title[:80],
                                                           "description": (description or "")[:1500] or None,
                                                           "visibility": "hidden"})
        renewal = kind == "subscription"
        variant_body = {"account_id": aid, "product_id": product["id"], "currency": self.settings.whop_currency.lower(),
                        "billing_type": "renewal" if renewal else "one_time", "visibility": "hidden", "sku": sku}
        if renewal:
            variant_body |= {"renewal_price": float(price_eur), "billing_period": 30}
        else:
            variant_body["initial_price"] = float(price_eur)
        variant = self.request("POST", "/variants", body=variant_body)
        local = self._local.create_product(sku, title, price_eur, kind, description, unit_cost_eur, vendor, asset_url)
        with session() as s:
            s.scalars(select(Product).where(Product.sku == sku)).one().external_id = str(variant["id"])
        return {**local, "whop_product_id": product["id"], "whop_variant_id": variant["id"],
                "purchase_url": variant.get("purchase_url")}

    def update_price(self, sku: str, price_eur: float) -> dict[str, Any]:
        variant_id, p = self._variant_of(sku)
        field = "renewal_price" if p["kind"] == "subscription" else "initial_price"
        self.request("PATCH", f"/variants/{variant_id}", body={field: float(price_eur)})
        return self._local.update_price(sku, price_eur)

    def set_status(self, sku: str, status: str) -> dict[str, Any]:
        """``active`` makes the variant and its product visible (purchasable), ``draft`` hides both, ``archived``
        archives the variant (the product stays, it may have other variants)."""
        if status not in VISIBILITY:
            raise ValueError(f"status must be one of {', '.join(VISIBILITY)}")
        variant_id, _ = self._variant_of(sku)
        v = self.request("PATCH", f"/variants/{variant_id}", body={"visibility": VISIBILITY[status]})
        pid = (v.get("product") or {}).get("id")
        if pid and status in ("active", "draft"):
            self.request("PATCH", f"/products/{pid}", body={"visibility": VISIBILITY[status]})
        return self._local.set_status(sku, status)

    def add_license_keys(self, sku: str, keys: list[str]) -> int:
        raise NotImplementedError("add_license_keys: Whop memberships carry their own licence keys "
                                  "(each purchase creates one); there is no key stock to fill")

    def create_order(self, customer_email: str, sku: str, country: str = "DE", campaign_id: str | None = None):
        raise NotImplementedError("create_order: orders are placed through Whop checkout (create a checkout link "
                                  "with whop_request checkout_configurations.create) and mirrored from "
                                  "payment.succeeded")

    # ---- fulfilment: done by Whop
    def deliver_order(self, order_id: str) -> dict[str, Any]:
        """No-op. Whop grants the membership and emails the buyer itself."""
        return {"order_id": order_id, "status": "delivered", "already": True, "delivered_by": PROVIDER}

    def verify_download(self, order_id: str, token: str) -> bool:
        return self._local.verify_download(order_id, token)

    def mark_email_sent(self, order_id: str) -> None:
        self._local.mark_email_sent(order_id)

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        return self.request("GET", f"/payments/{payment_id}")

    def refund_order(self, order_id: str, amount_eur: float, reason: str) -> dict[str, Any]:
        """Refund through ``POST /payments/{id}/refund`` (``partial_amount`` in major units of the payment currency),
        then mirror the returned payment. A 409 (conflict, e.g. already refunded) re-reads and mirrors the payment.
        The later ``refund.*`` webhook is a no-op because the mirror is idempotent."""
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            if not o.external_id:
                raise ValueError(f"order {order_id} is not a Whop order")
            ext, status = o.external_id, o.status
            remaining = cents(o.amount_eur) - cents(s.scalar(select(func.coalesce(func.sum(Refund.amount_eur), 0.0))
                                                             .where(Refund.order_id == order_id)))
        if status == "refunded":
            return {"order_id": order_id, "status": "refunded", "already": True}
        if not 0 < cents(amount_eur) <= remaining:
            raise ValueError(f"refund exceeds remaining amount: requested {amount_eur:.2f}, "
                             f"remaining {remaining / 100:.2f}")
        data = self.request("POST", f"/payments/{ext}/refund",
                            body={"partial_amount": float(amount_eur), "metadata": {"reason": reason[:500]}},
                            ok_statuses=(409,))
        if data.get("status_code") == 409:
            data = self.get_payment(ext)
        result = mirror_refund({"data": data}, reason=reason)
        return {**result, "amount_eur": amount_eur}
