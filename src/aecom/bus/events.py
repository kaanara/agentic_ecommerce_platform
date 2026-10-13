"""Persistent event bus.

Events are written to ``ops_events`` first (durable, visible on dashboards) and then drained to
in-process subscribers. Swap ``drain`` for a Redis/NATS consumer when moving beyond the PoC.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from sqlalchemy import select

from aecom.audit import audit
from aecom.db import EventRow, session

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], None]
_subscribers: dict[str, list[Handler]] = defaultdict(list)


def subscribe(topic: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        if fn not in _subscribers[topic]:
            _subscribers[topic].append(fn)
        return fn

    return deco


def publish(topic: str, payload: dict[str, Any], source: str = "system") -> int:
    with session() as s:
        row = EventRow(topic=topic, payload=payload, source=source)
        s.add(row)
        s.flush()
        return row.id


def drain(limit: int = 10_000) -> int:
    """Deliver unprocessed events to subscribers. Handlers may publish further events."""
    handled = 0
    for _ in range(limit):
        with session() as s:
            row = s.scalars(select(EventRow).where(EventRow.processed.is_(False)).order_by(EventRow.id).limit(1)).first()
            if row is None:
                break
            row.processed = True
            topic, payload = row.topic, dict(row.payload)
        for handler in _subscribers.get(topic, []):
            try:
                handler(payload)
            except Exception as exc:  # one bad handler must not stall the bus
                log.exception("handler %s failed for %s", handler.__name__, topic)
                audit("system", f"event:{topic}", "error", {"handler": handler.__name__, "error": str(exc)})
        handled += 1
    return handled
