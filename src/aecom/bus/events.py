"""Persistent event bus.

Events are written to ``ops_events`` first (durable, visible on dashboards) and then drained to
in-process subscribers, at least once, with retries and a dead letter. Swap ``drain`` for a Redis/NATS
consumer when moving beyond the PoC.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from aecom.audit import audit
from aecom.db import EventRow, now, session
from aecom.db import session as _session  # ``publish`` has a ``session`` keyword
from aecom.observability import record_error

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], None]


class HandlerFailed(RuntimeError):
    """Raised by a subscriber for an expected, retryable failure (logged without a traceback)."""
_subscribers: dict[str, list[Handler]] = defaultdict(list)


def subscribe(topic: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        if fn not in _subscribers[topic]:
            _subscribers[topic].append(fn)
        return fn

    return deco


_outbox: ContextVar[Session | None] = ContextVar("aecom_outbox", default=None)


@contextmanager
def outbox(s: Session) -> Iterator[Session]:
    """Transactional outbox: inside this block ``publish`` writes its event into ``s`` (committed with it)."""
    token = _outbox.set(s)
    try:
        yield s
    finally:
        _outbox.reset(token)


def publish(topic: str, payload: dict[str, Any], source: str = "system", *, session: Session | None = None) -> int:
    """Persist an event. With ``session`` (or inside ``outbox``) the event commits atomically with that session."""
    s = session if session is not None else _outbox.get()
    if s is not None:
        return _add_event(s, topic, payload, source)
    with _session() as s:
        return _add_event(s, topic, payload, source)


def _add_event(s: Session, topic: str, payload: dict[str, Any], source: str) -> int:
    row = EventRow(topic=topic, payload=payload, source=source)
    s.add(row)
    s.flush()
    return row.id


MAX_ATTEMPTS = 5
BACKOFF = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)]
LEASE = timedelta(minutes=15)  # a claimed event whose drain crashed becomes eligible again after this


def drain(limit: int = 10_000) -> int:
    """Deliver due events to subscribers, at least once. Handlers may publish further events.

    An event is claimed with a lease before its handlers run and marked processed only when every handler
    has succeeded. Handlers that already succeeded are skipped on retries, so a retry re-runs only what failed.
    Failures back off (BACKOFF); after MAX_ATTEMPTS the event is dead-lettered and an error is audited.
    Handlers signal failure by raising; they should be idempotent.
    """
    handled = 0
    for _ in range(limit):
        with session() as s:
            t = now()
            row = s.scalars(select(EventRow).where(
                EventRow.processed.is_(False), EventRow.dead.is_not(True),
                or_(EventRow.next_attempt_at.is_(None), EventRow.next_attempt_at <= t),
            ).order_by(EventRow.id).limit(1)).first()
            if row is None:
                break
            row.attempts = (row.attempts or 0) + 1
            row.next_attempt_at = t + LEASE
            event_id, topic, payload, attempt = row.id, row.topic, dict(row.payload), row.attempts
            done = list(row.done_handlers or [])
        errors: dict[str, str] = {}
        for handler in _subscribers.get(topic, []):
            if handler.__name__ in done:
                continue
            try:
                handler(payload)
                done.append(handler.__name__)
            except Exception as exc:  # one bad handler must not stall the bus
                if isinstance(exc, HandlerFailed):
                    log.warning("event %d %s: %s failed (attempt %d): %s", event_id, topic, handler.__name__, attempt, exc)
                else:
                    log.exception("handler %s failed for %s (attempt %d)", handler.__name__, topic, attempt)
                    record_error("event_bus", exc, event_id=event_id,
                                 context={"topic": topic, "handler": handler.__name__, "attempt": attempt})
                errors[handler.__name__] = f"{type(exc).__name__}: {exc}"
        with session() as s:
            row = s.get(EventRow, event_id)
            row.done_handlers = done
            if not errors:
                row.processed, row.next_attempt_at, row.last_error = True, None, None
            else:
                row.last_error = "; ".join(f"{h}: {e}" for h, e in errors.items())[:4000]
                if attempt >= MAX_ATTEMPTS:
                    row.dead, row.next_attempt_at = True, None
                else:
                    row.next_attempt_at = now() + BACKOFF[min(attempt, len(BACKOFF)) - 1]
                dead = row.dead
        if errors:
            audit("system", f"event:{topic}", "error",
                  {"event_id": event_id, "attempt": attempt, "dead_letter": dead, "errors": errors})
        handled += 1
    return handled


def status() -> dict[str, int]:
    """Event counts by state, for the CLI and dashboards."""
    from sqlalchemy import func

    with session() as s:
        def count(*where: Any) -> int:
            return s.scalar(select(func.count(EventRow.id)).where(*where)) or 0

        return {"waiting_for_retry": count(EventRow.processed.is_(False), EventRow.dead.is_not(True), EventRow.attempts > 0),
                "queued": count(EventRow.processed.is_(False), EventRow.dead.is_not(True), EventRow.attempts == 0),
                "dead_letter": count(EventRow.dead.is_(True))}


def dead_letters() -> list[dict[str, Any]]:
    with session() as s:
        return [{"id": e.id, "ts": e.ts, "topic": e.topic, "attempts": e.attempts, "last_error": e.last_error}
                for e in s.scalars(select(EventRow).where(EventRow.dead.is_(True)).order_by(EventRow.id))]


def retry(event_ids: list[int] | None = None) -> int:
    """Re-queue dead-lettered events (all of them if no ids are given), e.g. after fixing the cause."""
    with session() as s:
        q = select(EventRow).where(EventRow.dead.is_(True))
        if event_ids:
            q = q.where(EventRow.id.in_(event_ids))
        rows = list(s.scalars(q))
        for e in rows:
            e.dead, e.attempts, e.next_attempt_at = False, 0, None
        return len(rows)
