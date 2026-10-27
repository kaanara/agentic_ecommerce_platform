"""Built-in storefront backed by the company database. Used until a real platform is provisioned."""

from __future__ import annotations

import hmac
import secrets as pysecrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from aecom import ledger
from aecom.audit import audit
from aecom.bus.events import outbox, publish
from aecom.config import get_settings
from aecom.db import Customer, LicenseKey, Order, Product, Refund, now, session
from aecom.money import cents, quantize
from aecom.store.adapter import vat_included, vat_rate

# H8 (EU digital content): version of the checkout checkbox the customer ticks before immediate delivery.
# LEGAL REVIEW PENDING - bump the version whenever the wording changes; each order stores the version it agreed to.
WITHDRAWAL_CONSENT_VERSION = "2026-10-v1"
"""Consent wording of version 2026-10-v1 (English; to be reviewed by legal):

"I expressly agree that you start delivering the digital content before the end of the 14-day withdrawal period.
I acknowledge that I thereby lose my right of withdrawal once delivery has begun."
"""


def _p(p: Product) -> dict[str, Any]:
    return {"id": p.id, "sku": p.sku, "title": p.title, "kind": p.kind, "price_eur": p.price_eur,
            "unit_cost_eur": p.unit_cost_eur, "status": p.status, "vendor": p.vendor, "description": p.description}


def _o(o: Order) -> dict[str, Any]:
    return {"id": o.id, "customer_id": o.customer_id, "product_id": o.product_id, "amount_eur": o.amount_eur,
            "vat_eur": o.vat_eur, "status": o.status, "campaign_id": o.campaign_id, "email_sent": bool(o.email_sent),
            "withdrawal_consent_at": str(o.withdrawal_consent_at) if o.withdrawal_consent_at else None,
            "withdrawal_consent_version": o.withdrawal_consent_version, "created_at": str(o.created_at)}


def _now() -> datetime:
    """The clock, for methods whose ``now`` parameter shadows aecom.db.now."""
    return now()


def _download_link(o: Order) -> tuple[str, bool]:
    """The order's download link (I13): the same URL while it is valid, a new token and expiry once expired.

    Returns (url, reissued). The link names only the order and a random token, never Product.asset_url.
    """
    cfg = get_settings()
    reissued = False
    if o.download_token is None or o.download_expires_at is None or o.download_expires_at <= now():
        reissued = o.download_token is not None
        o.download_token = pysecrets.token_urlsafe(24)
        o.download_expires_at = now() + timedelta(days=cfg.download_link_days)
    return f"{cfg.download_base_url.rstrip('/')}/{o.id}/{o.download_token}", reissued


class MockStore:
    name = "mock"

    def list_products(self, status: str | None = None) -> list[dict[str, Any]]:
        with session() as s:
            q = select(Product)
            if status:
                q = q.where(Product.status == status)
            return [_p(p) for p in s.scalars(q)]

    def get_product(self, sku: str) -> dict[str, Any] | None:
        with session() as s:
            p = s.scalars(select(Product).where(Product.sku == sku)).first()
            return _p(p) if p else None

    def create_product(self, sku, title, price_eur, kind, description="", unit_cost_eur=0.0, vendor=None, asset_url=None):
        with session() as s:
            if s.scalars(select(Product).where(Product.sku == sku)).first():
                raise ValueError(f"SKU {sku} already exists")
            p = Product(sku=sku, title=title, price_eur=price_eur, kind=kind, description=description,
                        unit_cost_eur=unit_cost_eur, vendor=vendor, asset_url=asset_url, status="draft")
            s.add(p)
            s.flush()
            return _p(p)

    def update_price(self, sku: str, price_eur: float) -> dict[str, Any]:
        with session() as s:
            p = s.scalars(select(Product).where(Product.sku == sku)).one()
            p.price_eur = price_eur
            return _p(p)

    def set_status(self, sku: str, status: str) -> dict[str, Any]:
        with session() as s:
            p = s.scalars(select(Product).where(Product.sku == sku)).one()
            p.status = status
            return _p(p)

    def add_license_keys(self, sku: str, keys: list[str]) -> int:
        with session() as s:
            p = s.scalars(select(Product).where(Product.sku == sku)).one()
            for k in keys:
                s.add(LicenseKey(product_id=p.id, key=k))
        return len(keys)

    def inventory(self) -> list[dict[str, Any]]:
        with session() as s:
            rows = s.execute(
                select(Product.sku, Product.title, Product.kind, Product.status, func.count(LicenseKey.id))
                .join(LicenseKey, (LicenseKey.product_id == Product.id) & (LicenseKey.status == "available"), isouter=True)
                .group_by(Product.id)
            ).all()
        return [{"sku": r[0], "title": r[1], "kind": r[2], "status": r[3],
                 "available_keys": r[4] if r[2] == "license" else None} for r in rows]

    def create_order(self, customer_email: str, sku: str, country: str = "DE", campaign_id: str | None = None,
                     withdrawal_consent: bool = True, consent_version: str = WITHDRAWAL_CONSENT_VERSION):
        """Record a paid order. ``withdrawal_consent`` is the checkout checkbox (H8): without it the order is
        still paid, but deliver_order holds it as 'awaiting_consent' until a human or a later consent releases it."""
        # transactional outbox (I11): customer, order, ledger lines and order.paid commit together or not at all
        with session() as s, outbox(s):
            p = s.scalars(select(Product).where(Product.sku == sku)).one()
            if p.status != "active":
                raise ValueError(f"{sku} is not active")
            c = s.scalars(select(Customer).where(Customer.email == customer_email)).first()
            # tax at the customer's country; resolve it before writing so an unknown country leaves no rows
            tax_country = (c.country if c else country).upper()
            rate = vat_rate(tax_country)
            vat = vat_included(p.price_eur, tax_country)
            if c is None:
                c = Customer(email=customer_email, country=tax_country, acquisition_channel=campaign_id)
                s.add(c)
                s.flush()
            ordered_at = now()
            o = Order(customer_id=c.id, product_id=p.id, amount_eur=p.price_eur, vat_eur=vat,
                      campaign_id=campaign_id, status="paid", created_at=ordered_at,
                      withdrawal_consent_at=ordered_at if withdrawal_consent else None,
                      withdrawal_consent_version=consent_version if withdrawal_consent else None)
            s.add(o)
            s.flush()
            result = _o(o)
            ledger.record_sale(o.id, o.amount_eur, o.vat_eur, p.unit_cost_eur, country=tax_country, vat_rate=rate,
                               session=s)
            publish("order.paid", {"order_id": o.id, "sku": sku, "email": customer_email}, source="store")
        return result

    def deliver_order(self, order_id: str) -> dict[str, Any]:
        result = self._deliver(order_id)
        reissued = result.pop("_link_reissued", None)
        if reissued:  # audited after the delivery transaction committed (one SQLite writer); never the token
            audit("store", "download_link_reissued", "ok", {"order_id": order_id, "expires_at": reissued})
        return result

    def _deliver(self, order_id: str) -> dict[str, Any]:
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            p = s.get(Product, o.product_id)
            if o.status in ("partially_refunded", "refunded"):  # refunds end the lifecycle: no new key, no status change
                return {"order_id": order_id, "status": o.status, "already": True}
            if o.status == "delivered":  # idempotent; the access details let the caller (re)send the email
                key = s.scalars(select(LicenseKey).where(LicenseKey.order_id == order_id)).first()
                already: dict[str, Any] = {"order_id": order_id, "status": "delivered", "sku": p.sku,
                                           "license_key": key.key if key else None, "already": True}
                if p.kind != "license":  # I13: a resend carries the same link while it is valid
                    already["download_url"], reissued = _download_link(o)
                    if reissued:
                        already["_link_reissued"] = str(o.download_expires_at)
                return already
            if o.withdrawal_consent_at is None:  # H8: no immediate delivery without the customer's express consent
                o.status = "awaiting_consent"
                return {"order_id": order_id, "status": "awaiting_consent",
                        "error": "customer has not consented to immediate delivery (withdrawal right)"}
            delivered: dict[str, Any] = {"order_id": order_id, "status": "delivered", "sku": p.sku}
            remaining = None
            if p.kind == "license":
                key = s.scalars(select(LicenseKey).where(LicenseKey.product_id == p.id, LicenseKey.status == "available")).first()
                if key is None:
                    o.status = "failed"
                    delivered = {"order_id": order_id, "status": "failed", "error": "no licence keys available"}
                else:
                    key.status, key.order_id = "assigned", order_id
                    delivered["license_key"] = key.key
                    remaining = s.scalar(select(func.count(LicenseKey.id))
                                         .where(LicenseKey.product_id == p.id, LicenseKey.status == "available"))
            else:
                delivered["download_url"], _ = _download_link(o)
            if delivered["status"] == "delivered":
                o.status = "delivered"
        # publish only after the write transaction above has committed (SQLite allows one writer)
        if delivered["status"] == "failed":
            publish("inventory.out_of_stock", {"sku": p.sku, "order_id": order_id}, source="store")
        elif remaining is not None and remaining < 5:
            publish("inventory.low", {"sku": p.sku, "available_keys": remaining}, source="store")
        return delivered

    def verify_download(self, order_id: str, token: str, now: datetime | None = None) -> bool:
        """True when ``token`` is the order's current, unexpired download token (I13). A fully refunded order
        loses access, a partially refunded one keeps it. Constant-time compare; the token is never logged."""
        at = now if now is not None else _now()
        with session() as s:
            o = s.get(Order, order_id)
            if o is None or not o.download_token or o.download_expires_at is None or not token:
                return False
            if o.status not in ("delivered", "partially_refunded") or o.download_expires_at <= at:
                return False
            return hmac.compare_digest(o.download_token.encode(), str(token).encode())

    def record_withdrawal_consent(self, order_id: str,
                                  consent_version: str = WITHDRAWAL_CONSENT_VERSION) -> dict[str, Any]:
        """Record a consent given after checkout (H8); an 'awaiting_consent' order becomes 'paid' again so it can
        be delivered. Does not deliver by itself."""
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            if o.withdrawal_consent_at is None:
                o.withdrawal_consent_at, o.withdrawal_consent_version = now(), consent_version
            if o.status == "awaiting_consent":
                o.status = "paid"
            return _o(o)

    def mark_email_sent(self, order_id: str) -> None:
        """Record that the access email of a delivered order went out (I4), so it is never sent twice."""
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            o.email_sent, o.email_sent_at = True, now()

    def refund_order(self, order_id: str, amount_eur: float, reason: str) -> dict[str, Any]:
        """Full or partial refund; the licence key is revoked only once the whole amount is refunded.

        Refund row, order status, ledger lines and the order.refunded event commit in one transaction (I11).
        """
        with session() as s, outbox(s):
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            if o.status == "refunded":
                return {"order_id": order_id, "status": "refunded", "already": True}
            refunded = s.scalar(select(func.coalesce(func.sum(Refund.amount_eur), 0.0))
                                .where(Refund.order_id == order_id))
            remaining = cents(o.amount_eur) - cents(refunded)
            if not 0 < cents(amount_eur) <= remaining:
                raise ValueError(f"refund exceeds remaining amount: requested {amount_eur:.2f}, "
                                 f"remaining {remaining / 100:.2f}")
            country, rate = self._tax(s, o)
            s.add(Refund(order_id=order_id, amount_eur=float(quantize(amount_eur)), reason=reason))
            full = cents(amount_eur) == remaining
            o.status = "refunded" if full else "partially_refunded"
            if full:
                for key in s.scalars(select(LicenseKey).where(LicenseKey.order_id == order_id)):
                    key.status = "revoked"
            status = o.status
            ledger.record_refund(order_id, amount_eur, country=country, vat_rate=rate, session=s)
            publish("order.refunded", {"order_id": order_id, "amount_eur": amount_eur, "status": status}, source="store")
        return {"order_id": order_id, "status": status, "amount_eur": amount_eur,
                "remaining_eur": (remaining - cents(amount_eur)) / 100}

    @staticmethod
    def _tax(s, o: Order) -> tuple[str | None, float]:
        """Tax country and rate of an order: the customer's country, else derived from the booked VAT."""
        c = s.get(Customer, o.customer_id)
        country = c.country.upper() if c and c.country else None
        try:
            return country, vat_rate(country or "")
        except ValueError:
            net = o.amount_eur - o.vat_eur
            return country, (o.vat_eur / net if net else 0.0)

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                return None
            c = s.get(Customer, o.customer_id)
            return {**_o(o), "customer_email": c.email if c else None}

    def list_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        with session() as s:
            return [_o(o) for o in s.scalars(select(Order).order_by(Order.created_at.desc()).limit(limit))]
