"""Built-in storefront backed by the company database. Used until a real platform is provisioned."""

from __future__ import annotations

import secrets as pysecrets
from typing import Any

from sqlalchemy import func, select

from aecom import ledger
from aecom.bus.events import publish
from aecom.db import Customer, LicenseKey, Order, Product, Refund, session
from aecom.store.adapter import vat_included, vat_rate


def _p(p: Product) -> dict[str, Any]:
    return {"id": p.id, "sku": p.sku, "title": p.title, "kind": p.kind, "price_eur": p.price_eur,
            "unit_cost_eur": p.unit_cost_eur, "status": p.status, "vendor": p.vendor, "description": p.description}


def _o(o: Order) -> dict[str, Any]:
    return {"id": o.id, "customer_id": o.customer_id, "product_id": o.product_id, "amount_eur": o.amount_eur,
            "vat_eur": o.vat_eur, "status": o.status, "campaign_id": o.campaign_id, "created_at": str(o.created_at)}


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

    def create_order(self, customer_email: str, sku: str, country: str = "DE", campaign_id: str | None = None):
        with session() as s:
            p = s.scalars(select(Product).where(Product.sku == sku)).one()
            if p.status != "active":
                raise ValueError(f"{sku} is not active")
            c = s.scalars(select(Customer).where(Customer.email == customer_email)).first()
            # tax at the customer's country; resolve it before writing so an unknown country leaves no rows
            tax_country = (c.country if c else country).upper()
            rate = vat_rate(tax_country)
            vat = vat_included(p.price_eur, tax_country)
            if c is None:
                c = Customer(email=customer_email, country=country, acquisition_channel=campaign_id)
                s.add(c)
                s.flush()
            o = Order(customer_id=c.id, product_id=p.id, amount_eur=p.price_eur,
                      vat_eur=vat, campaign_id=campaign_id, status="paid")
            s.add(o)
            s.flush()
            result, unit_cost = _o(o), p.unit_cost_eur
        ledger.record_sale(result["id"], result["amount_eur"], result["vat_eur"], unit_cost,
                           country=tax_country, vat_rate=rate)
        publish("order.paid", {"order_id": result["id"], "sku": sku, "email": customer_email}, source="store")
        return result

    def deliver_order(self, order_id: str) -> dict[str, Any]:
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            if o.status == "delivered":  # idempotent
                key = s.scalars(select(LicenseKey).where(LicenseKey.order_id == order_id)).first()
                return {"order_id": order_id, "status": "delivered", "license_key": key.key if key else None, "already": True}
            p = s.get(Product, o.product_id)
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
                delivered["download_url"] = f"https://downloads.example/{p.sku}/{pysecrets.token_urlsafe(12)}"
            if delivered["status"] == "delivered":
                o.status = "delivered"
        # publish only after the write transaction above has committed (SQLite allows one writer)
        if delivered["status"] == "failed":
            publish("inventory.out_of_stock", {"sku": p.sku, "order_id": order_id}, source="store")
        elif remaining is not None and remaining < 5:
            publish("inventory.low", {"sku": p.sku, "available_keys": remaining}, source="store")
        return delivered

    def refund_order(self, order_id: str, amount_eur: float, reason: str) -> dict[str, Any]:
        with session() as s:
            o = s.get(Order, order_id)
            if o is None:
                raise ValueError(f"order {order_id} not found")
            if o.status == "refunded":
                return {"order_id": order_id, "status": "refunded", "already": True}
            if amount_eur > o.amount_eur:
                raise ValueError("refund exceeds order amount")
            s.add(Refund(order_id=order_id, amount_eur=amount_eur, reason=reason))
            o.status = "refunded"
            for key in s.scalars(select(LicenseKey).where(LicenseKey.order_id == order_id)):
                key.status = "revoked"
        ledger.record_refund(order_id, amount_eur)
        publish("order.refunded", {"order_id": order_id, "amount_eur": amount_eur}, source="store")
        return {"order_id": order_id, "status": "refunded", "amount_eur": amount_eur}

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
