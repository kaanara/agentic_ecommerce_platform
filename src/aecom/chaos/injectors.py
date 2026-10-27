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
def email_failures(rate: float, seed: int = 0) -> Iterator[dict[str, int]]:
    """AgentMail.send raises RuntimeError on a seeded ``rate`` share of calls (the mail provider is flaky)."""
    from aecom.integrations.agentmail import AgentMail

    rng, stats, real = random.Random(seed), {"calls": 0, "failures": 0}, AgentMail.send

    @functools.wraps(real)
    def flaky(self: Any, to: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        stats["calls"] += 1
        if rng.random() < rate:
            stats["failures"] += 1
            raise RuntimeError(f"chaos: email to {to} failed")
        return real(self, to, *args, **kwargs)

    with _swap(AgentMail, "send", flaky):
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


# --------------------------------------------------------------------------- Lemon Squeezy webhooks
WEBHOOK_RANK = {"order_refunded": 0, "license_key_created": 1, "license_key_updated": 1, "order_created": 2}


def _event(payload: dict[str, Any]) -> str:
    return (payload.get("meta") or {}).get("event_name", "")


def ls_schedule(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The order a Lemon Squeezy-like provider sends its webhooks in: as they happened. Swapped by ``out_of_order``."""
    return list(payloads)


@contextmanager
def duplicate_webhooks() -> Iterator[dict[str, int]]:
    """Every Lemon Squeezy webhook is delivered twice (``lemonsqueezy.dispatch`` runs twice per delivery)."""
    from aecom.store import lemonsqueezy

    stats, real = {"duplicates": 0}, lemonsqueezy.dispatch

    @functools.wraps(real)
    def twice(payload: dict[str, Any]) -> dict[str, Any]:
        first = real(payload)
        real(payload)
        stats["duplicates"] += 1
        return first

    with _swap(lemonsqueezy, "dispatch", twice):
        yield stats


@contextmanager
def out_of_order(seed: int = 0) -> Iterator[dict[str, int]]:
    """Webhooks arrive in the worst order: every refund before any ``order_created`` and licence keys before their
    order (``mirror_license_key`` then fails and the provider retries it), shuffled within each kind."""
    rng, stats = random.Random(seed), {"reordered": 0}

    def worst(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        shuffled = list(payloads)
        rng.shuffle(shuffled)
        out = sorted(shuffled, key=lambda p: WEBHOOK_RANK.get(_event(p), 3))
        stats["reordered"] += sum(a is not b for a, b in zip(out, payloads, strict=True))
        return out

    with _swap(sys.modules[__name__], "ls_schedule", worst):
        yield stats


@contextmanager
def lost_webhooks(rate: float, seed: int = 0) -> Iterator[dict[str, int]]:
    """A seeded ``rate`` share of Lemon Squeezy webhooks never arrives (dropped before ``dispatch``)."""
    from aecom.store import lemonsqueezy

    rng, stats, real = random.Random(seed), {"deliveries": 0, "lost": 0}, lemonsqueezy.dispatch

    @functools.wraps(real)
    def lossy(payload: dict[str, Any]) -> dict[str, Any]:
        stats["deliveries"] += 1
        if rng.random() < rate:
            stats["lost"] += 1
            return {"event": _event(payload), "status": "lost"}
        return real(payload)

    with _swap(lemonsqueezy, "dispatch", lossy):
        yield stats


@contextmanager
def lemonsqueezy_api(orders: list[dict[str, Any]], countries: dict[str, str] | None = None,
                     page_size: int = 2) -> Iterator[dict[str, int]]:
    """An in-process Lemon Squeezy API: ``GET /v1/orders`` serves ``orders`` (JSON:API order objects) newest first,
    ``page_size`` per page; ``GET /v1/customers/:id`` serves ``countries``. No API key and no network:
    ``LemonSqueezyStore._client`` is swapped for a client on an ``httpx.MockTransport``."""
    import httpx

    from aecom.store.lemonsqueezy import LemonSqueezyStore

    stats = {"requests": 0, "order_pages": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        stats["requests"] += 1
        path = request.url.path.removeprefix("/v1")
        if path == "/orders":
            stats["order_pages"] += 1
            ordered = sorted(orders, key=lambda o: o["attributes"]["created_at"], reverse=True)
            number = int(request.url.params.get("page[number]", 1))
            last = max(1, -(-len(ordered) // page_size))
            page = ordered[(number - 1) * page_size:number * page_size]
            return httpx.Response(200, json={"data": page, "meta": {"page": {"currentPage": number, "lastPage": last}}})
        cid = path.removeprefix("/customers/")
        if cid != path and cid in (countries or {}):
            return httpx.Response(200, json={"data": {"type": "customers", "id": cid,
                                                      "attributes": {"country": countries[cid]}}})
        return httpx.Response(404, json={"errors": [{"detail": "not found"}]})

    def client(self: Any) -> httpx.Client:
        return httpx.Client(base_url="https://api.lemonsqueezy.test/v1", transport=httpx.MockTransport(handler))

    with _swap(LemonSqueezyStore, "_client", client):
        yield stats
