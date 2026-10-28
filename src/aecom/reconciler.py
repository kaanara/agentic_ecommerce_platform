"""Order reconciler: the safety net for paid orders whose ``order.paid`` event was lost or dead-lettered,
and for delivered orders whose access email never went out (I4). With ``STORE_ADAPTER=lemonsqueezy`` it also
recovers lost Lemon Squeezy webhooks: recent provider orders are compared with the local mirror (G2).

Runs on a schedule (see aecom.scheduler). Pure bookkeeping: never calls an LLM.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from aecom.audit import audit
from aecom.bus.events import publish
from aecom.config import get_settings
from aecom.db import Customer, EventRow, Order, Product, now, session

UNDELIVERED = ("paid", "failed")  # failed = no licence keys at delivery time
UNEMAILED = ("delivered", "partially_refunded")  # entitled orders whose access email may still be owed
DELIVERY_TOPICS = ("order.paid", "inventory.out_of_stock")  # both end in deliver_order
PROVIDER_WINDOW = timedelta(days=2)  # how far back provider orders are compared with the mirror


def reconcile(older_than: timedelta = timedelta(minutes=15)) -> dict[str, int]:
    """Re-publish ``order.paid`` for undelivered orders older than ``older_than`` with no delivery event pending.

    Delivered orders whose email was never sent are requeued the same way (deliver_order is idempotent and only
    resends the email); they are counted as ``requeued_email``, a key present only when there are some.
    With the Lemon Squeezy store, ``LemonSqueezyStore.reconcile_recent`` runs over the last ``PROVIDER_WINDOW`` and its
    counts are added with an ``ls_`` prefix; a failure there is recorded (``ls_errors``), never raised.
    Idempotent: a requeued event stays pending until drained, so the next run skips that order.
    """
    cutoff = now() - older_than
    with session() as s:
        base = (select(Order.id, Product.sku, Customer.email)
                .join(Product, Product.id == Order.product_id).join(Customer, Customer.id == Order.customer_id)
                .where(Order.created_at < cutoff).order_by(Order.created_at))
        orders = s.execute(base.where(Order.status.in_(UNDELIVERED))).all()
        unemailed = s.execute(base.where(Order.status.in_(UNEMAILED), Order.email_sent.is_not(True))).all()
        pending = {(e.payload or {}).get("order_id") for e in s.scalars(select(EventRow).where(
            EventRow.topic.in_(DELIVERY_TOPICS), EventRow.processed.is_(False), EventRow.dead.is_not(True)))}
        dead = {(e.payload or {}).get("order_id"): e.id for e in s.scalars(select(EventRow).where(
            EventRow.topic == "order.paid", EventRow.dead.is_(True)).order_by(EventRow.id))}
    counts = {"checked": len(orders) + len(unemailed), "requeued": 0, "already_pending": 0}
    for kind, rows in (("requeued", orders), ("requeued_email", unemailed)):
        for order_id, sku, email in rows:
            if order_id in pending:
                counts["already_pending"] += 1
                continue
            if order_id in dead:  # revive the dead-lettered event rather than leaving it behind next to a copy
                event_id = dead[order_id]
                with session() as s:
                    e = s.get(EventRow, event_id)
                    e.dead, e.attempts, e.next_attempt_at = False, 0, None
            else:
                event_id = publish("order.paid", {"order_id": order_id, "sku": sku, "email": email, "reconciled": True},
                                   source="reconciler")
            audit("system", "reconcile", "info", {"order_id": order_id, "event_id": event_id, "reason": kind})
            counts[kind] = counts.get(kind, 0) + 1
    if get_settings().store_adapter == "lemonsqueezy":
        counts.update(_reconcile_provider())
    return counts


def _reconcile_provider() -> dict[str, int]:
    from aecom.observability import record_error
    from aecom.store.lemonsqueezy import LemonSqueezyStore

    try:
        found = LemonSqueezyStore().reconcile_recent(now() - PROVIDER_WINDOW)
    except Exception as exc:
        record_error("reconciler.lemonsqueezy", exc)
        return {"ls_errors": 1}
    if found["mirrored"] or found["refunds"]:
        audit("system", "reconcile", "info", {"provider": "lemonsqueezy", **found})
    return {f"ls_{k}": v for k, v in found.items()}
