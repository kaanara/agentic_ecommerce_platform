"""Order reconciler: the safety net for paid orders whose ``order.paid`` event was lost or dead-lettered,
and for delivered orders whose access email never went out (I4).

Runs on a schedule (see aecom.scheduler). Pure bookkeeping: never calls an LLM.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aecom.audit import audit
from aecom.bus.events import publish
from aecom.db import Customer, EventRow, Order, Product, now, session

UNDELIVERED = ("paid", "failed")  # failed = no licence keys at delivery time
DELIVERY_TOPICS = ("order.paid", "inventory.out_of_stock")  # both end in deliver_order


def reconcile(older_than: timedelta = timedelta(minutes=15)) -> dict[str, int]:
    """Re-publish ``order.paid`` for undelivered orders older than ``older_than`` with no delivery event pending.

    Delivered orders whose email was never sent are requeued the same way (deliver_order is idempotent and only
    resends the email); they are counted as ``requeued_email``, a key present only when there are some.
    Idempotent: a requeued event stays pending until drained, so the next run skips that order.
    """
    cutoff = now() - older_than
    with session() as s:
        base = (select(Order.id, Product.sku, Customer.email)
                .join(Product, Product.id == Order.product_id).join(Customer, Customer.id == Order.customer_id)
                .where(Order.created_at < cutoff).order_by(Order.created_at))
        orders = s.execute(base.where(Order.status.in_(UNDELIVERED))).all()
        unemailed = s.execute(base.where(Order.status == "delivered", Order.email_sent.is_not(True))).all()
        pending = {(e.payload or {}).get("order_id") for e in s.scalars(select(EventRow).where(
            EventRow.topic.in_(DELIVERY_TOPICS), EventRow.processed.is_(False), EventRow.dead.is_not(True)))}
    counts = {"checked": len(orders) + len(unemailed), "requeued": 0, "already_pending": 0}
    for kind, rows in (("requeued", orders), ("requeued_email", unemailed)):
        for order_id, sku, email in rows:
            if order_id in pending:
                counts["already_pending"] += 1
                continue
            event_id = publish("order.paid", {"order_id": order_id, "sku": sku, "email": email, "reconciled": True},
                               source="reconciler")
            audit("system", "reconcile", "info", {"order_id": order_id, "event_id": event_id, "reason": kind})
            counts[kind] = counts.get(kind, 0) + 1
    return counts
