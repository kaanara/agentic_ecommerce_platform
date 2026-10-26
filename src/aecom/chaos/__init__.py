"""Chaos testing: inject faults into a real (scripted) run, then assert the business invariants still hold.

``run_scenario(name, seed)`` works on whatever database is configured, so point DATABASE_URL at a scratch DB
(tests use a fresh one per test). Agents always run in scripted mode here: no API calls, no spend, reproducible.
"""

from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable, Iterator

from aecom.chaos import injectors
from aecom.chaos.invariants import check_all

MAX_ROUNDS = 10
RECONCILER_CYCLES = 2  # phase1-exit: how many reconciler runs may be needed to repair what retries could not
COUNTRIES = ["DE", "FR", "NL", "AT", "IT", "ES", "PL", "SE", "US", "GB"]


@contextmanager
def _chaos_run() -> Iterator[None]:
    """Scripted LLM and zero retry backoff for the duration of a scenario, restored afterwards."""
    import aecom.handlers  # noqa: F401  (register event subscribers)
    from aecom.bus import events
    from aecom.config import get_settings

    settings = get_settings()
    saved = settings.llm_mode, events.BACKOFF
    settings.llm_mode, events.BACKOFF = "scripted", [timedelta(0)] * len(events.BACKOFF)
    try:
        yield
    finally:
        settings.llm_mode, events.BACKOFF = saved


def settle() -> int:
    """Drain the bus until no event is due (or MAX_ROUNDS); returns events handled."""
    from aecom.bus.events import drain

    total = 0
    for _ in range(MAX_ROUNDS):
        handled = drain()
        total += handled
        if not handled:
            break
    return total


def _place_orders(rng: random.Random, count: int) -> list[str]:
    from aecom.bootstrap import seed_demo
    from aecom.store.adapter import get_store

    seed_demo()
    store = get_store()
    skus = sorted(p["sku"] for p in store.list_products("active"))
    return [store.create_order(f"chaos{i}@example.com", rng.choice(skus), rng.choice(COUNTRIES))["id"]
            for i in range(count)]


def _delivery_crash(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    with injectors.delivery_failures(0.3, seed) as stats:
        _place_orders(rng, orders)
        settle()
    return {"orders": orders, **stats}


def _duplicate_paid(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    with injectors.duplicate_order_paid() as stats:
        _place_orders(rng, orders)
        settle()
    return {"orders": orders, **stats}


def _email_failures(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    with injectors.email_failures(0.3, seed) as stats:
        _place_orders(rng, orders)
        settle()
    return {"orders": orders, **stats}


def _phase1_exit(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    """Phase 1 exit gate: deliveries and emails fail at once; bus retries plus the reconciler must repair it all."""
    from aecom.reconciler import reconcile

    with injectors.delivery_failures(0.3, seed) as delivery, \
            injectors.email_failures(0.3, rng.randrange(2**32)) as email:
        _place_orders(rng, orders)
        settle()
        cycles = 0
        while check_all() and cycles < RECONCILER_CYCLES:  # the scheduled reconciler, simulated
            cycles += 1
            reconcile(older_than=timedelta(0))
            settle()
    return {"orders": orders, "delivery_failures": delivery["failures"], "email_failures": email["failures"],
            "reconciler_cycles": cycles}


def _claude_down(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    from aecom.bus.events import publish, retry

    _place_orders(rng, max(orders, 1))
    settle()
    with injectors.claude_down() as stats:  # support can't think: the email retries, then is dead-lettered
        publish("email.received", {"from": "chaos0@example.com", "subject": "Licence key not working",
                                   "body": "Hi, my key says invalid. Can you help?"}, source="chaos")
        settle()
    requeued = retry()  # the API is back: an operator re-queues the dead letters
    settle()
    return {"orders": max(orders, 1), "model_calls_failed": stats["calls"], "requeued": requeued}


SCENARIOS: dict[str, tuple[str, Callable[[random.Random, int, int], dict[str, Any]]]] = {
    "delivery-crash": ("30% of deliveries crash; at-least-once retries must still deliver every order", _delivery_crash),
    "duplicate-paid": ("every order.paid arrives twice; no order may get two keys", _duplicate_paid),
    "email-failures": ("30% of access emails fail to send; retries must still email every delivered order",
                       _email_failures),
    "phase1-exit": ("Phase 1 exit gate: 30% delivery and 30% email failures at once; retries and up to two "
                    "reconciler cycles must leave zero violations", _phase1_exit),
    "claude-down": ("the model API is down while a customer email arrives; it is answered after recovery", _claude_down),
}


def run_scenario(name: str, seed: int = 0, orders: int = 20) -> dict[str, Any]:
    """Run one chaos scenario on the configured DB; returns {scenario, seed, violations, stats}."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown chaos scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    with _chaos_run():
        stats = SCENARIOS[name][1](random.Random(seed), seed, orders)
    return {"scenario": name, "seed": seed, "violations": check_all(), "stats": stats}
