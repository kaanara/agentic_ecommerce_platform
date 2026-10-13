"""Market simulator — stands in for real traffic until a real storefront and ad platforms are provisioned.

Live campaigns spend budget and convert into orders; active products also get organic and repeat orders.
Orders flow through the real store adapter, ledger and event bus, so every downstream agent behaves as in production.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from aecom import ledger
from aecom.bus.events import drain, publish
from aecom.db import Campaign, Customer, LedgerEntry, Order, Product, now, session
from aecom.store.adapter import get_store

COUNTRIES = ["DE", "DE", "DE", "FR", "NL", "AT", "IT", "ES", "PL", "SE", "US", "GB"]
CHANNEL_PROFILE = {  # (ctr, cvr, cpc_eur)
    "google_ads": (0.035, 0.050, 0.60), "meta_ads": (0.018, 0.005, 0.35), "tiktok_ads": (0.012, 0.010, 0.20),
    "linkedin_ads": (0.008, 0.030, 2.10), "newsletter": (0.040, 0.060, 0.0), "organic_social": (0.010, 0.020, 0.0),
}
SUPPORT_TEMPLATES = [
    ("Licence key not working", "Hi, the key for my order {order} says invalid. Can you help?"),
    ("Refund please", "Hello, I bought {sku} by mistake (order {order}). Could I get a refund? Thanks"),
    ("Download link expired", "My download link for order {order} expired, please resend."),
]


def _backdate(order_id: str, ts: Any) -> None:
    with session() as s:
        o = s.get(Order, order_id)
        o.created_at = ts
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order_id)):
            e.created_at = ts


def _order(rng: random.Random, sku: str, email: str | None, campaign_id: str | None, ts: Any) -> dict[str, Any] | None:
    email = email or f"buyer{rng.randint(10000, 99999)}@example.com"
    try:
        o = get_store().create_order(email, sku, country=rng.choice(COUNTRIES), campaign_id=campaign_id)
    except ValueError:
        return None
    _backdate(o["id"], ts)
    return o


def simulate_day(day_offset: int = 0, seed: int | None = None, support_emails: bool = False) -> dict[str, Any]:
    """Simulate one day of traffic. day_offset < 0 backfills history for dashboards."""
    rng = random.Random(seed)
    ts = now() + timedelta(days=day_offset)
    stats = {"orders": 0, "ad_spend": 0.0, "emails": 0}
    with session() as s:
        live = [(c.id, c.channel, c.budget_eur - c.spent_eur, c.product_id, c.budget_eur)
                for c in s.scalars(select(Campaign).where(Campaign.status == "live"))]
        products = {p.id: p.sku for p in s.scalars(select(Product).where(Product.status == "active"))}
        customers = list(s.scalars(select(Customer.email)))
    for cid, channel, remaining, product_id, budget in live:
        ctr, cvr, cpc = CHANNEL_PROFILE.get(channel, (0.015, 0.02, 0.5))
        spend = round(min(remaining, budget / 14 * rng.uniform(0.8, 1.2)), 2)  # campaigns pace over ~2 weeks
        clicks = int(spend / cpc) if cpc else rng.randint(40, 160)
        impressions = int(clicks / ctr)
        conversions = sum(1 for _ in range(clicks) if rng.random() < cvr)
        sku = products.get(product_id) or (rng.choice(list(products.values())) if products else None)
        revenue = 0.0
        for _ in range(conversions if sku else 0):
            if o := _order(rng, sku, None, cid, ts):
                revenue += o["amount_eur"] - o["vat_eur"]
                stats["orders"] += 1
        if spend > 0:
            txn = ledger.record_expense("marketing_expense", spend, f"ad spend {channel}", cid)
            with session() as s:
                for e in s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)):
                    e.created_at = ts
            stats["ad_spend"] += spend
        with session() as s:
            c = s.get(Campaign, cid)
            c.spent_eur += spend
            c.impressions += impressions
            c.clicks += clicks
            c.conversions += conversions
            c.revenue_eur += revenue
            ended = c.spent_eur >= c.budget_eur - 0.01
            if ended:
                c.status = "ended"
            roas = round(c.revenue_eur / c.spent_eur, 2) if c.spent_eur else None
        if ended:
            publish("campaign.ended", {"campaign_id": cid, "roas": roas})
    for sku in products.values():  # organic + repeat purchases
        for _ in range(rng.randint(0, 3)):
            email = rng.choice(customers) if customers and rng.random() < 0.18 else None
            if _order(rng, sku, email, None, ts):
                stats["orders"] += 1
    if support_emails and products:
        with session() as s:
            recent = list(s.scalars(select(Order).where(Order.status == "delivered").order_by(Order.created_at.desc()).limit(20)))
            picks = [(o.id, s.get(Customer, o.customer_id).email, s.get(Product, o.product_id).sku) for o in recent]
        if picks and rng.random() < 0.6:
            oid, email, sku = rng.choice(picks)
            subject, body = rng.choice(SUPPORT_TEMPLATES)
            publish("email.received", {"from": email, "subject": subject, "body": body.format(order=oid, sku=sku)}, source="simulator")
            stats["emails"] += 1
    stats["events"] = drain(limit=10_000)
    return stats
