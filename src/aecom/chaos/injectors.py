"""Fault injectors: context managers that monkeypatch module attributes for the duration of a block.

No network, no real outages: each fault is simulated in-process and undone on exit. Randomised faults use
``random.Random(seed)`` so a run is reproducible. Each injector yields a stats dict it updates as faults fire.
"""

from __future__ import annotations

import functools
import importlib
import random
import sqlite3
import sys
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy.exc import OperationalError


@contextmanager
def _swap(owner: Any, name: str, new: Any) -> Iterator[None]:
    """Replace ``owner.name``, plus aliases imported by name into other aecom modules (``from x import f``)."""
    old = getattr(owner, name)
    targets = [(owner, name)]
    if not isinstance(owner, type):
        targets += [(m, name) for key, m in list(sys.modules.items())
                    if key.startswith("aecom") and m is not owner and getattr(m, name, None) is old]
    for obj, attr in targets:
        setattr(obj, attr, new)
    try:
        yield
    finally:
        for obj, attr in targets:
            setattr(obj, attr, old)


@contextmanager
def delivery_failures(rate: float, seed: int = 0) -> Iterator[dict[str, int]]:
    """MockStore.deliver_order raises RuntimeError on a seeded ``rate`` share of calls."""
    from aecom.store.mock import MockStore

    rng, stats, real = random.Random(seed), {"calls": 0, "failures": 0}, MockStore.deliver_order

    @functools.wraps(real)
    def flaky(self: Any, order_id: str) -> dict[str, Any]:
        stats["calls"] += 1
        if rng.random() < rate:
            stats["failures"] += 1
            raise RuntimeError(f"chaos: delivery of {order_id} failed")
        return real(self, order_id)

    with _swap(MockStore, "deliver_order", flaky):
        yield stats


@contextmanager
def duplicate_order_paid() -> Iterator[dict[str, int]]:
    """Every published ``order.paid`` is published twice (a payment webhook delivered twice)."""
    from aecom.bus import events

    stats, real = {"duplicates": 0}, events.publish

    @functools.wraps(real)
    def twice(topic: str, payload: dict[str, Any], source: str = "system") -> int:
        event_id = real(topic, payload, source)
        if topic == "order.paid":
            real(topic, dict(payload), source)
            stats["duplicates"] += 1
        return event_id

    with _swap(events, "publish", twice):
        yield stats


@contextmanager
def claude_down() -> Iterator[dict[str, int]]:
    """The model API is unreachable: aecom.graph.agents.get_model raises."""
    from aecom.graph import agents

    stats = {"calls": 0}

    def unavailable(*_a: Any, **_k: Any) -> Any:
        stats["calls"] += 1
        raise RuntimeError("chaos: Claude API unavailable")

    with _swap(agents, "get_model", unavailable):
        yield stats


def _resolve(fn_path: str) -> tuple[Any, str]:
    """'aecom.ledger.post' or 'aecom.store.mock.MockStore.deliver_order' -> (owner, attribute)."""
    parts = fn_path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        try:
            owner: Any = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for attr in parts[i:-1]:
            owner = getattr(owner, attr)
        if not hasattr(owner, parts[-1]):
            raise AttributeError(f"{fn_path} not found")
        return owner, parts[-1]
    raise ImportError(f"cannot import {fn_path}")


@contextmanager
def db_locked_once(fn_path: str) -> Iterator[dict[str, int]]:
    """The first call of ``fn_path`` raises OperationalError('database is locked'); later calls go through."""
    owner, name = _resolve(fn_path)
    real, lock, stats = getattr(owner, name), threading.Lock(), {"calls": 0, "failures": 0}

    @functools.wraps(real)
    def locked_once(*args: Any, **kwargs: Any) -> Any:
        with lock:
            stats["calls"] += 1
            first = stats["calls"] == 1
            stats["failures"] += first
        if first:
            raise OperationalError("chaos", None, sqlite3.OperationalError("database is locked"))
        return real(*args, **kwargs)

    with _swap(owner, name, locked_once):
        yield stats
