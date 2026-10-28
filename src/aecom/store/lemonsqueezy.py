"""Lemon Squeezy storefront (STORE_ADAPTER=lemonsqueezy). Lemon Squeezy is the system of record (Roadmap L1).

Lemon Squeezy is the merchant of record. It runs checkout and payment, charges and files EU VAT, generates
licence keys, hosts downloads, emails receipts and access, and processes refunds. This app **mirrors** that data
into its own tables and ledger, so agents, monitors and reports keep working unchanged:

* ``sync_catalog()`` reads products and variants over the REST API and upserts one local ``Product`` per variant
  (``Product.external_id`` = variant id).
* ``mirror_order`` / ``mirror_refund`` / ``mirror_license_key`` are pure functions on webhook payloads
  (``meta`` + JSON:API ``data``). The webhook route calls them with its own ``session=`` so the receipt and the
  mirror commit (or roll back) together. Without ``session`` each one commits its writes and its event in one
  transaction. All are idempotent on the Lemon Squeezy id.
* Mirrored orders are already ``delivered`` with ``email_sent`` set, because Lemon Squeezy delivers keys and files
  and sends the receipt itself. They publish ``order.mirrored``, never ``order.paid``, so nothing is delivered twice.

Products and prices are managed in the Lemon Squeezy dashboard. ``create_product``, ``update_price``,
``set_status``, ``add_license_keys`` and ``create_order`` raise ``NotImplementedError`` with a message saying so.
Agents then get an ERROR result and can ask a human. Refunds use the API (``POST /v1/orders/:id/refund``,
amount in cents; https://docs.lemonsqueezy.com/api/orders/issue-refund).

API docs: https://docs.lemonsqueezy.com/api. Webhooks: https://docs.lemonsqueezy.com/help/webhooks.
Full licence keys are never stored or logged. A ``LicenseKey`` row keeps only the Lemon Squeezy key id and the last
4 characters.
"""

from __future__ import annotations

import hashlib
import hmac
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Iterator

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aecom import ledger
from aecom.audit import mask_detail
from aecom.bus.events import HandlerFailed, outbox, publish
from aecom.config import get_settings
from aecom.db import AuditLog, Customer, ErrorRow, LicenseKey, Order, Product, Refund, now, session
from aecom.money import cents, quantize
from aecom.observability import mask
from aecom.secrets import secrets
from aecom.store.mock import MockStore

PROVIDER = "lemonsqueezy"
JSONAPI = "application/vnd.api+json"
MANAGED_IN_DASHBOARD = ("Products, variants and prices are managed in the Lemon Squeezy dashboard (system of record). "
                        "Ask a human to change it there, then run sync_catalog().")
# our checkout passes these in meta.custom_data (Lemon Squeezy has no field for them; see tests/fixtures/lemonsqueezy)
CUSTOM_COUNTRY = "country"
CUSTOM_CONSENT_AT = "withdrawal_consent_at"
CUSTOM_CONSENT_VERSION = "withdrawal_consent_version"
CUSTOM_CAMPAIGN = "campaign_id"
CUSTOM_MARKETING_CONSENT = "marketing_consent"  # explicit opt-in checkbox at checkout; nothing else counts as consent
REVOKED_KEY_STATUSES = {"disabled", "expired"}


# --------------------------------------------------------------------------- helpers
def _eur(amount_cents: int | str | None) -> Decimal:
    return quantize(Decimal(int(amount_cents or 0)) / 100)


def _ts(value: str | None) -> datetime | None:
    """ISO 8601 (``...Z``) as a naive UTC datetime, like the rest of the database."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
    except ValueError:
        return None


def _custom(payload: dict[str, Any]) -> dict[str, Any]:
    return (payload.get("meta") or {}).get("custom_data") or {}


def _resource(payload: dict[str, Any], kind: str) -> tuple[str, dict[str, Any]]:
    data = payload.get("data") or {}
    if data.get("type") != kind:
        raise ValueError(f"expected a Lemon Squeezy '{kind}' resource, got '{data.get('type')}'")
    return str(data["id"]), data.get("attributes") or {}


def _tax_rate(a: dict[str, Any]) -> float:
    """Lemon Squeezy sends the rate as a percentage string ("21.00"); no tax -> 0."""
    if not a.get("tax"):
        return 0.0
    try:
        return float(Decimal(str(a.get("tax_rate") or 0)) / 100)
    except ArithmeticError:
        return 0.0


def _opted_in(value: Any) -> bool:
    """Checkout custom data arrives as JSON booleans or as strings (``checkout[custom][...]`` query values)."""
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1", "yes"))


def _refunded_c(s: Session, order_id: str) -> int:
    return cents(s.scalar(select(func.coalesce(func.sum(Refund.amount_eur), 0.0)).where(Refund.order_id == order_id)))


def _mirrored(s: Session, external_id: str) -> Order | None:
    return s.scalars(select(Order).where(Order.external_id == external_id)).first()


def _o(o: Order) -> dict[str, Any]:
    return {"order_id": o.id, "external_id": o.external_id, "status": o.status, "amount_eur": o.amount_eur,
            "vat_eur": o.vat_eur}


@contextmanager
def _tx(s: Session | None) -> Iterator[Session]:
    """The caller's session (it commits) or a fresh committed one; events go through the outbox of that session."""
    if s is not None:
        with outbox(s):
            yield s
    else:
        with session() as own, outbox(own):
            yield own


def verify_signature(raw_body: bytes, signature: str | None, secret: str | None = None) -> bool:
    """``X-Signature`` is the hex HMAC-SHA256 of the raw body under the webhook signing secret
    (https://docs.lemonsqueezy.com/help/webhooks/signing-requests). No secret configured -> reject."""
    secret = secret if secret is not None else secrets.get("LEMONSQUEEZY_WEBHOOK_SECRET")
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


# --------------------------------------------------------------------------- mirroring (webhook payloads)
def _create_mirror(s: Session, ext: str, a: dict[str, Any], payload: dict[str, Any], country: str | None) -> Order:
    """Customer, order and sale ledger lines for a Lemon Squeezy order, inside the caller's transaction."""
    currency = str(a.get("currency") or "").upper()
    if currency != get_settings().lemonsqueezy_currency.upper():
        raise ValueError(f"Lemon Squeezy order {ext} is in {currency or '?'}, the ledger books "
                         f"{get_settings().lemonsqueezy_currency}; not converted, mirror it manually")
    custom = _custom(payload)
    item = a.get("first_order_item") or {}
    gross, vat, rate = _eur(a.get("total")), _eur(a.get("tax")), _tax_rate(a)
    tax_country = (country or custom.get(CUSTOM_COUNTRY) or "").upper() or None
    variant_id = str(item.get("variant_id") or "")
    p = s.scalars(select(Product).where(Product.external_id == variant_id)).first() if variant_id else None
    if p is None:  # sold before the catalog sync saw it: keep the sale, the next sync_catalog fills in the details
        p = Product(sku=f"LS-{variant_id or ext}", title=item.get("product_name") or f"Lemon Squeezy {variant_id}",
                    price_eur=float(_eur(item.get("price") or a.get("total"))), kind="download",
                    external_id=variant_id or None, status="active")
        s.add(p)
        s.flush()
    email = a.get("user_email") or ""
    c = s.scalars(select(Customer).where(Customer.email == email)).first()
    campaign = custom.get(CUSTOM_CAMPAIGN)
    if c is None:
        c = Customer(email=email, name=a.get("user_name") or "", country=tax_country or "", acquisition_channel=campaign)
        s.add(c)
        s.flush()
    elif tax_country and not c.country:
        c.country = tax_country
    if _opted_in(custom.get(CUSTOM_MARKETING_CONSENT)) and not c.marketing_consent:
        c.marketing_consent, c.marketing_consent_source = True, "checkout"
        c.marketing_consent_at = _ts(a.get("created_at")) or now()
    o = Order(customer_id=c.id, product_id=p.id, quantity=max(int(item.get("quantity") or 1), 1),
              amount_eur=float(gross), vat_eur=float(vat), campaign_id=campaign,
              status="delivered", email_sent=True, email_sent_at=now(), external_id=ext,
              withdrawal_consent_at=_ts(custom.get(CUSTOM_CONSENT_AT)),
              withdrawal_consent_version=custom.get(CUSTOM_CONSENT_VERSION) if custom.get(CUSTOM_CONSENT_AT) else None,
              created_at=_ts(a.get("created_at")) or now())
    s.add(o)
    s.flush()
    # VAT exactly as Lemon Squeezy charged it (merchant of record), never recomputed from our rate table
    ledger.record_sale(o.id, float(gross), float(vat), p.unit_cost_eur, country=tax_country, vat_rate=rate, session=s)
    publish("order.mirrored", {"order_id": o.id, "external_id": ext, "sku": p.sku, "email": email,
                               "amount_eur": o.amount_eur, "vat_eur": o.vat_eur}, source=PROVIDER, session=s)
    return o


def mirror_order(payload: dict[str, Any], country: str | None = None, *, session: Session | None = None) -> dict[str, Any]:
    """Mirror an ``order_created`` payload of a paid order. Idempotent on the Lemon Squeezy order id.

    ``country`` (ISO alpha-2) overrides ``meta.custom_data.country``. The order object carries no customer country.
    ``meta.custom_data.marketing_consent`` true (the checkout opt-in) records marketing consent on the customer
    (source ``checkout``); absent or false changes nothing. Orders that are not ``paid`` (pending, failed,
    fraudulent) are ignored. Refund states go through ``mirror_refund``. The order quantity is
    ``first_order_item.quantity``. With ``session`` everything is written there and the caller commits.
    """
    ext, a = _resource(payload, "orders")
    if a.get("status") != "paid":
        return {"external_id": ext, "status": "ignored", "reason": f"order status {a.get('status')}"}
    with _tx(session) as s:
        existing = _mirrored(s, ext)
        if existing is not None:
            return {**_o(existing), "already": True}
        return _o(_create_mirror(s, ext, a, payload, country))


def _post_refund(s: Session, o: Order, refund_c: int, cumulative_c: int) -> None:
    """Reverse the sale pro rata on Lemon Squeezy's own tax, so refunds adding up to the order reverse exactly
    the VAT booked on the sale. (``ledger.record_refund`` re-splits by rate, which can differ from the MoR's tax
    by a cent.) The memo has the same format as record_refund, so reports and ``_refund_basis`` read it the same way."""
    country, rate, _prior_gross, prior_vat = ledger._refund_basis(o.id, s)
    total_c = cents(o.amount_eur)
    vat_cum = quantize(Decimal(cents(o.vat_eur)) * cumulative_c / total_c / 100) if total_c else Decimal(0)
    g = Decimal(refund_c) / 100
    vat = vat_cum - prior_vat
    lines: list[tuple] = [("refunds", g - vat, 0), ("cash", 0, g)]
    if vat:
        lines.append(("vat_payable", vat, 0, country, rate))
    ledger.post(lines, f"refund {o.id}", o.id, session=s)


def mirror_refund(payload: dict[str, Any], country: str | None = None,
                  reason: str = "refunded in Lemon Squeezy", *, session: Session | None = None) -> dict[str, Any]:
    """Mirror an ``order_refunded`` payload (full or partial). Lemon Squeezy's cumulative ``refunded_amount`` is
    authoritative: only the part not mirrored yet is booked, so replays and out-of-order deliveries change nothing.
    An order seen for the first time here is mirrored first."""
    ext, a = _resource(payload, "orders")
    with _tx(session) as s:
        o = _mirrored(s, ext) or _create_mirror(s, ext, a, payload, country)
        total_c = cents(o.amount_eur)
        target_c = min(int(a.get("refunded_amount") or 0), total_c)
        if a.get("refunded") and not target_c:
            target_c = total_c
        done_c = _refunded_c(s, o.id)
        delta_c = target_c - done_c
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


def _reject_extra_key(s: Session, o: Order, key_id: str, held: int) -> dict[str, Any]:
    """A licence key beyond the order's quantity is not stored: error row and audit entry, written into ``s``
    (the caller's transaction, so no second writer is opened on SQLite)."""
    detail = {"license_key_id": key_id, "order_id": o.id, "external_id": o.external_id, "quantity": o.quantity or 1,
              "keys_held": held}
    msg = f"licence key {key_id} rejected: order {o.id} already holds {held} of {o.quantity or 1} key(s)"
    s.add(ErrorRow(component="lemonsqueezy.license_key", exc_type="ValueError", message=mask(msg),
                   context=mask_detail(detail)))
    s.add(AuditLog(actor=PROVIDER, action="license_key_over_quantity", decision="rejected", detail=mask_detail(detail)))
    return {"license_key_id": key_id, "order_id": o.id, "status": "rejected", "reason": "license_key_over_quantity",
            "already": False}


def mirror_license_key(payload: dict[str, Any], *, session: Session | None = None) -> dict[str, Any]:
    """Mirror ``license_key_created`` / ``license_key_updated``. Only the key id and the last 4 characters are kept
    (``ls:<key id>:****<last4>``). The customer gets the key from Lemon Squeezy. Raises ``HandlerFailed``
    (retryable) while the order is not mirrored yet, because Lemon Squeezy sends this event alongside ``order_created``.
    ``Order.quantity`` comes from the order item and is never raised here: a key beyond it is rejected
    (``license_key_over_quantity``, error row + audit entry)."""
    key_id, a = _resource(payload, "license-keys")
    ext = str(a.get("order_id") or "")
    raw = str(a.get("key") or a.get("key_short") or "")
    last4 = raw[-4:]
    del raw  # never keep, log or return the full key
    revoked = bool(a.get("disabled")) or a.get("status") in REVOKED_KEY_STATUSES
    with _tx(session) as s:
        o = _mirrored(s, ext)
        if o is None:
            raise HandlerFailed(f"licence key {key_id}: Lemon Squeezy order {ext} is not mirrored yet")
        prefix = f"ls:{key_id}:"
        row = s.scalars(select(LicenseKey).where(LicenseKey.key.startswith(prefix))).first()
        already = row is not None
        if row is None:
            held = s.scalar(select(func.count(LicenseKey.id)).where(LicenseKey.order_id == o.id)) or 0
            if held >= (o.quantity or 1):
                return _reject_extra_key(s, o, key_id, held)
            row = LicenseKey(product_id=o.product_id, key=f"{prefix}****{last4}", order_id=o.id)
            s.add(row)
        row.status = "revoked" if revoked or o.status == "refunded" else "assigned"
        return {"license_key_id": key_id, "order_id": o.id, "key_last4": last4, "status": row.status,
                "already": already}


EVENT_HANDLERS = {
    "order_created": mirror_order,
    "order_refunded": mirror_refund,
    "license_key_created": mirror_license_key,
    "license_key_updated": mirror_license_key,
}


def dispatch(payload: dict[str, Any], *, session: Session | None = None) -> dict[str, Any]:
    """Route a verified webhook body by ``meta.event_name``. Events we do not mirror are acknowledged and ignored.
    With ``session`` the handler writes into it and the caller commits (the webhook route: receipt + mirror)."""
    event = (payload.get("meta") or {}).get("event_name", "")
    handler = EVENT_HANDLERS.get(event)
    if handler is None:
        return {"event": event, "status": "ignored"}
    return {"event": event, **handler(payload, session=session)}


# --------------------------------------------------------------------------- adapter
class LemonSqueezyStore:
    """``StoreAdapter`` backed by Lemon Squeezy. Reads come from the local mirror (kept current by webhooks and
    ``sync_catalog``). Catalog writes are refused, because the Lemon Squeezy dashboard owns the catalog."""

    name = PROVIDER

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = get_settings()
        self._transport = transport  # tests inject httpx.MockTransport; production uses the network
        self._local = MockStore()

    # ---- REST API
    def _client(self) -> httpx.Client:
        key = secrets.get("LEMONSQUEEZY_API_KEY")
        if not key:
            raise RuntimeError("LEMONSQUEEZY_API_KEY is not set. A human provisions it (request_provisioning) "
                               "and puts it in .env.")
        return httpx.Client(base_url=self.settings.lemonsqueezy_base_url, timeout=self.settings.lemonsqueezy_timeout_s,
                            transport=self._transport,
                            headers={"Accept": JSONAPI, "Content-Type": JSONAPI, "Authorization": f"Bearer {key}"})

    @staticmethod
    def _get_all(c: httpx.Client, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """All pages of a JSON:API list (``page[number]`` until ``meta.page.lastPage``)."""
        out: list[dict[str, Any]] = []
        number = 1
        while True:
            r = c.get(path, params={**params, "page[number]": number, "page[size]": 100})
            r.raise_for_status()
            body = r.json()
            out.extend(body.get("data") or [])
            last = ((body.get("meta") or {}).get("page") or {}).get("lastPage") or 1
            if number >= last:
                return out
            number += 1

    def _store_id(self) -> str:
        sid = self.settings.lemonsqueezy_store_id
        if not sid:
            raise RuntimeError("LEMONSQUEEZY_STORE_ID is not set")
        return str(sid)

    def sync_catalog(self) -> dict[str, Any]:
        """Upsert one local product per Lemon Squeezy variant (``external_id`` = variant id).

        New variants get the SKU ``LS-<variant id>``. Existing rows keep their SKU, unit cost and vendor. A variant is
        ``active`` when its product is published and the variant is published. A product's only variant (the
        "default" one, which has status ``pending``) also counts.
        """
        with self._client() as c:
            products = self._get_all(c, "/products", {"filter[store_id]": self._store_id()})
            variants = {p["id"]: self._get_all(c, "/variants", {"filter[product_id]": p["id"]}) for p in products}
        created = updated = 0
        with session() as s:
            for prod in products:
                pa = prod.get("attributes") or {}
                vs = variants[prod["id"]]
                for v in vs:
                    va = v.get("attributes") or {}
                    sellable = pa.get("status") == "published" and (
                        va.get("status") == "published" or (va.get("status") == "pending" and len(vs) == 1))
                    name = va.get("name") or ""
                    title = pa.get("name", "") if len(vs) == 1 or name in ("", "Default") else f"{pa.get('name', '')} - {name}"
                    row = s.scalars(select(Product).where(Product.external_id == str(v["id"]))).first()
                    if va.get("has_license_keys"):
                        kind = "license"
                    elif va.get("is_subscription"):
                        kind = "subscription"
                    else:
                        kind = row.kind if row is not None and row.kind not in ("license", "subscription") else "download"
                    fields = {"title": title, "price_eur": float(_eur(va.get("price"))), "kind": kind,
                              "description": va.get("description") or pa.get("description") or "",
                              "status": "active" if sellable else "draft", "asset_url": pa.get("buy_now_url")}
                    if row is None:
                        s.add(Product(sku=f"LS-{v['id']}", external_id=str(v["id"]), **fields))
                        created += 1
                    else:
                        for k, val in fields.items():
                            setattr(row, k, val)
                        updated += 1
        return {"products": len(products), "variants": sum(len(v) for v in variants.values()),
                "created": created, "updated": updated}

    def customer_country(self, ls_customer_id: int | str) -> str | None:
        """Country of a Lemon Squeezy customer (``GET /v1/customers/:id``). Pass it to ``mirror_order(country=)``
        when the checkout did not put ``country`` into custom data."""
        with self._client() as c:
            r = c.get(f"/customers/{ls_customer_id}")
            r.raise_for_status()
            return ((r.json().get("data") or {}).get("attributes") or {}).get("country")

    # ---- lost-webhook recovery
    def recent_orders(self, since: datetime) -> list[dict[str, Any]]:
        """Orders created at or after ``since`` (naive UTC), from ``GET /v1/orders?filter[store_id]=...``.
        The list is newest first, so paging stops at the first page that reaches back past ``since``."""
        out: list[dict[str, Any]] = []
        with self._client() as c:
            number = 1
            while True:
                r = c.get("/orders", params={"filter[store_id]": self._store_id(), "page[number]": number,
                                             "page[size]": 100})
                r.raise_for_status()
                body = r.json()
                page = body.get("data") or []
                fresh = [o for o in page if (_ts((o.get("attributes") or {}).get("created_at")) or since) >= since]
                out.extend(fresh)
                last = ((body.get("meta") or {}).get("page") or {}).get("lastPage") or 1
                if number >= last or len(fresh) < len(page) or not page:
                    return out
                number += 1

    def reconcile_recent(self, since: datetime) -> dict[str, int]:
        """Repair lost or failed webhooks: mirror every recent Lemon Squeezy order that is missing locally, and
        book the rest of any refund whose local total is below the provider's ``refunded_amount``.

        Idempotent: the mirror functions only book what is not mirrored yet, so a second run changes nothing.
        A failing order is recorded (``aecom.observability.record_error``) and counted in ``errors``; the others go on.
        The order object has no checkout custom data, so the country is looked up on the Lemon Squeezy customer.
        """
        from aecom.observability import record_error

        orders = self.recent_orders(since)
        counts = {"listed": len(orders), "mirrored": 0, "refunds": 0, "unchanged": 0, "ignored": 0, "errors": 0,
                  "keys": 0, "key_errors": 0}
        for data in orders:
            ext, a = str(data.get("id")), data.get("attributes") or {}
            try:
                with session() as s:
                    o = _mirrored(s, ext)
                    local_c = _refunded_c(s, o.id) if o is not None else 0
                    total_c = cents(o.amount_eur) if o is not None else int(a.get("total") or 0)
                provider_c = min(int(a.get("refunded_amount") or 0), total_c)
                if a.get("refunded") and not provider_c:
                    provider_c = total_c
                if o is None and a.get("status") not in ("paid", "refunded", "partial_refund"):
                    counts["ignored"] += 1  # pending / failed / fraudulent: mirror_order would ignore it as well
                    continue
                if o is None:
                    payload = {"meta": {"event_name": "order_created"}, "data": data}
                    country = self._country_or_none(a.get("customer_id"))
                    if provider_c:  # mirror_refund mirrors the order first
                        mirror_refund(payload, country=country)
                        counts["refunds"] += 1
                    else:
                        mirror_order(payload, country=country)
                    counts["mirrored"] += 1
                elif provider_c > local_c:
                    mirror_refund({"meta": {"event_name": "order_refunded"}, "data": data})
                    counts["refunds"] += 1
                else:
                    counts["unchanged"] += 1
            except Exception as exc:
                counts["errors"] += 1
                record_error("lemonsqueezy.reconcile", exc, context={"external_id": ext})
        for ext in self._orders_missing_keys([str(d.get("id")) for d in orders]):
            try:
                counts["keys"] += self._recover_keys(ext)
            except Exception as exc:  # a failing key lookup never blocks order recovery
                counts["key_errors"] += 1
                record_error("lemonsqueezy.reconcile_keys", exc, context={"external_id": ext})
        return counts

    @staticmethod
    def _orders_missing_keys(externals: list[str]) -> list[str]:
        """Mirrored orders of licence products that hold fewer ``LicenseKey`` rows than their quantity."""
        if not externals:
            return []
        with session() as s:
            rows = s.execute(select(Order.external_id, Order.quantity, func.count(LicenseKey.id))
                             .join(Product, Product.id == Order.product_id)
                             .outerjoin(LicenseKey, LicenseKey.order_id == Order.id)
                             .where(Order.external_id.in_(externals), Product.kind == "license",
                                    Order.status != "refunded")
                             .group_by(Order.id, Order.external_id, Order.quantity).order_by(Order.id)).all()
        return [ext for ext, qty, held in rows if held < (qty or 1)]

    def _recover_keys(self, ext: str) -> int:
        """Mirror the licence keys of one order from ``GET /v1/license-keys?filter[order_id]=`` (a lost
        ``license_key_created`` webhook). Idempotent by Lemon Squeezy key id; keys stay masked. Returns keys added."""
        with self._client() as c:
            keys = self._get_all(c, "/license-keys", {"filter[order_id]": ext})
        added = 0
        for key in keys:
            r = mirror_license_key({"meta": {"event_name": "license_key_created"}, "data": key})
            added += 0 if r.get("already") or r.get("status") == "rejected" else 1
        return added

    def _country_or_none(self, ls_customer_id: Any) -> str | None:
        if not ls_customer_id:
            return None
        try:
            return self.customer_country(ls_customer_id)
        except httpx.HTTPError:
            return None  # the sale is still booked; its VAT line just has no country

    # ---- local mirror reads
    def list_products(self, status: str | None = None) -> list[dict[str, Any]]:
        return self._local.list_products(status)

    def get_product(self, sku: str) -> dict[str, Any] | None:
        return self._local.get_product(sku)

    def inventory(self) -> list[dict[str, Any]]:
        """Lemon Squeezy generates licence keys per purchase, so there is no local key stock to run out of
        (``available_keys`` is None and the restock check skips these rows)."""
        return [{**item, "available_keys": None} for item in self._local.inventory()]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self._local.get_order(order_id)
        if order is not None:
            with session() as s:
                order["external_id"] = s.get(Order, order_id).external_id
        return order

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._local.list_orders(limit)

    # ---- managed in the Lemon Squeezy dashboard
    def create_product(self, sku, title, price_eur, kind, description="", unit_cost_eur=0.0, vendor=None, asset_url=None):
        raise NotImplementedError(f"create_product: {MANAGED_IN_DASHBOARD}")

    def update_price(self, sku: str, price_eur: float) -> dict[str, Any]:
        raise NotImplementedError(f"update_price: {MANAGED_IN_DASHBOARD}")

    def set_status(self, sku: str, status: str) -> dict[str, Any]:
        raise NotImplementedError(f"set_status: {MANAGED_IN_DASHBOARD}")

    def add_license_keys(self, sku: str, keys: list[str]) -> int:
        raise NotImplementedError("add_license_keys: Lemon Squeezy generates licence keys per purchase "
                                  "(enable 'license keys' on the variant in the dashboard)")

    def create_order(self, customer_email: str, sku: str, country: str = "DE", campaign_id: str | None = None):
        raise NotImplementedError("create_order: orders are placed through Lemon Squeezy checkout and mirrored "
                                  "from its order_created webhook")

    # ---- fulfilment: done by Lemon Squeezy
    def deliver_order(self, order_id: str) -> dict[str, Any]:
        """No-op. Lemon Squeezy has already delivered the key or file and emailed the customer."""
        return {"order_id": order_id, "status": "delivered", "already": True, "delivered_by": PROVIDER}

    def mark_email_sent(self, order_id: str) -> None:
        self._local.mark_email_sent(order_id)

    def refund_order(self, order_id: str, amount_eur: float, reason: str) -> dict[str, Any]:
        """Refund through the API (``POST /v1/orders/:id/refund``, ``amount`` in cents), then mirror the returned
        order. The later ``order_refunded`` webhook is a no-op because the mirror is idempotent."""
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            if not o.external_id:
                raise ValueError(f"order {order_id} is not a Lemon Squeezy order")
            ext, status = o.external_id, o.status
            remaining = cents(o.amount_eur) - cents(s.scalar(select(func.coalesce(func.sum(Refund.amount_eur), 0.0))
                                                             .where(Refund.order_id == order_id)))
        if status == "refunded":
            return {"order_id": order_id, "status": "refunded", "already": True}
        if not 0 < cents(amount_eur) <= remaining:
            raise ValueError(f"refund exceeds remaining amount: requested {amount_eur:.2f}, "
                             f"remaining {remaining / 100:.2f}")
        body = {"data": {"type": "orders", "id": ext, "attributes": {"amount": cents(amount_eur)}}}
        with self._client() as c:
            r = c.post(f"/orders/{ext}/refund", json=body)
            r.raise_for_status()
            data = r.json().get("data") or {}
        result = mirror_refund({"meta": {"event_name": "order_refunded"}, "data": data}, reason=reason)
        return {**result, "amount_eur": amount_eur}
