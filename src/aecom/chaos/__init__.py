"""Chaos testing: inject faults into a real (scripted) run, then assert the business invariants still hold.

``run_scenario(name, seed)`` works on whatever database is configured, so point DATABASE_URL at a scratch DB
(tests use a fresh one per test). Agents always run in scripted mode here: no API calls, no spend, reproducible.
"""

from __future__ import annotations

import copy
import random
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Callable, Iterator

from aecom.chaos import injectors
from aecom.chaos.invariants import STORE_CHECKS, Violation, check_all, refund_mirror_matches_provider

MAX_ROUNDS = 10
RECONCILER_CYCLES = 2  # phase1-exit: how many reconciler runs may be needed to repair what retries could not
COUNTRIES = ["DE", "FR", "NL", "AT", "IT", "ES", "PL", "SE", "US", "GB"]
# Lemon Squeezy chaos: (variant id, product name, price in cents, issues licence keys) and VAT % by country
LS_VARIANTS = [(101, "FocusFlow Pro", 4900, True), (201, "AI Automation Course", 2900, False),
               (301, "Prompt Pack", 990, False)]
LS_VAT = {"DE": 19, "FR": 20, "NL": 21, "AT": 20, "IT": 22, "ES": 21, "PL": 23, "SE": 25}
LS_RETRIES = 4  # delivery attempts per webhook before the simulated provider gives up
LS_LOST_RATE = 0.3


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


# --------------------------------------------------------------------------- Lemon Squeezy webhooks
@contextmanager
def _lemonsqueezy_store() -> Iterator[None]:
    """STORE_ADAPTER=lemonsqueezy (EUR store "chaos") for the duration of a scenario, restored afterwards."""
    from aecom.config import get_settings

    settings = get_settings()
    saved = settings.store_adapter, settings.lemonsqueezy_store_id, settings.lemonsqueezy_currency
    settings.store_adapter, settings.lemonsqueezy_store_id, settings.lemonsqueezy_currency = "lemonsqueezy", "chaos", "EUR"
    try:
        yield
    finally:
        settings.store_adapter, settings.lemonsqueezy_store_id, settings.lemonsqueezy_currency = saved


def _ls_hook(event: str, data: dict[str, Any], custom: dict[str, Any]) -> dict[str, Any]:
    return {"meta": {"test_mode": True, "event_name": event, "custom_data": dict(custom)}, "data": copy.deepcopy(data)}


def _ls_world(rng: random.Random, count: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """A provider's view of ``count`` recent orders, shaped like tests/fixtures/lemonsqueezy: the final order objects
    (as ``GET /v1/orders`` lists them), the webhooks in the order they happened, and customer countries by id.
    Some orders are refunded: partially, fully, or partially and later the rest."""
    from aecom.db import now

    start = now() - timedelta(hours=count + 1)
    orders, hooks, countries = [], [], {}
    for i in range(count):
        variant, name, price, licensed = rng.choice(LS_VARIANTS)
        country = rng.choice(sorted(LS_VAT))
        rate = LS_VAT[country]
        created = (start + timedelta(minutes=37 * i)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        ext, customer = str(9000 + i), 7000 + i
        a = {"store_id": "chaos", "customer_id": customer, "order_number": 2000 + i, "user_name": f"LS Chaos {i}",
             "user_email": f"ls{i}@example.com", "currency": "EUR", "tax": round(price * rate / (100 + rate)),
             "total": price, "refunded_amount": 0, "tax_name": "VAT", "tax_rate": f"{rate}.00", "tax_inclusive": True,
             "status": "paid", "refunded": False, "refunded_at": None, "created_at": created, "updated_at": created,
             "first_order_item": {"order_id": int(ext), "product_id": variant // 10, "variant_id": variant,
                                  "product_name": name, "variant_name": "Default", "price": price}}
        order = {"type": "orders", "id": ext, "attributes": a}
        custom: dict[str, Any] = {"country": country}
        if rng.random() < 0.5:
            custom["marketing_consent"] = True
        hooks.append(_ls_hook("order_created", order, custom))
        if licensed:
            key = {"type": "license-keys", "id": str(3000 + i),
                   "attributes": {"order_id": int(ext), "customer_id": customer, "status": "inactive", "disabled": 0,
                                  "key": f"CHAOS{i:04d}-fake-{rng.randrange(10**4):04d}", "created_at": created}}
            hooks.append(_ls_hook("license_key_created", key, custom))
        kind = rng.choice(["none", "none", "none", "none", "partial", "full", "partial+rest"])
        if kind in ("partial", "partial+rest"):
            a.update(refunded_amount=price // 3, status="partial_refund")
            hooks.append(_ls_hook("order_refunded", order, custom))
        if kind in ("full", "partial+rest"):
            a.update(refunded_amount=price, status="refunded", refunded=True)
            hooks.append(_ls_hook("order_refunded", order, custom))
        orders.append(order)
        countries[str(customer)] = country
    return orders, hooks, countries


def _ls_deliver(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send webhooks like the provider does, through ``lemonsqueezy.dispatch`` in ``injectors.ls_schedule`` order.
    A delivery that fails retryably (``HandlerFailed``) is retried after the others, up to LS_RETRIES attempts.
    Returns the deliveries that never got through."""
    from aecom.bus.events import HandlerFailed
    from aecom.store import lemonsqueezy

    queue = injectors.ls_schedule(payloads)
    for _ in range(LS_RETRIES):
        failed = []
        for payload in queue:
            try:
                lemonsqueezy.dispatch(payload)
            except HandlerFailed:
                failed.append(payload)
        queue = failed
        if not queue:
            break
    return queue


def _ls_checks(orders: list[dict[str, Any]], undelivered: list[dict[str, Any]]) -> list[Violation]:
    """Store-platform invariants plus the provider comparison; a webhook the provider gave up on is a violation."""
    provider = {o["id"]: o["attributes"]["refunded_amount"] for o in orders}
    out = [v for check in STORE_CHECKS.values() for v in check()] + refund_mirror_matches_provider(provider)
    out += [{"invariant": "webhooks_delivered", "detail": f"{p['meta']['event_name']} {p['data']['id']} was never "
             f"processed after {LS_RETRIES} attempts"} for p in undelivered]
    return out


def _ls_duplicate(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    world, hooks, _ = _ls_world(rng, max(orders // 2, 3))
    with _lemonsqueezy_store(), injectors.duplicate_webhooks() as stats:
        undelivered = _ls_deliver(hooks)
        settle()
        violations = _ls_checks(world, undelivered)
    return {"orders": len(world), "webhooks": len(hooks), **stats, "store_violations": violations}


def _ls_out_of_order(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    world, hooks, _ = _ls_world(rng, max(orders // 2, 3))
    with _lemonsqueezy_store(), injectors.out_of_order(seed) as stats:
        undelivered = _ls_deliver(hooks)
        settle()
        violations = _ls_checks(world, undelivered)
    return {"orders": len(world), "webhooks": len(hooks), **stats, "store_violations": violations}


def _ls_lost(rng: random.Random, seed: int, orders: int) -> dict[str, Any]:
    """30% of webhooks are lost. The reconciler (reconcile_recent against the provider API) repairs the mirror, then
    the provider's later retries of deliveries that failed meanwhile (licence keys of a lost order) go through."""
    from aecom.reconciler import reconcile

    world, hooks, countries = _ls_world(rng, max(orders // 2, 3))
    with _lemonsqueezy_store():
        with injectors.lost_webhooks(LS_LOST_RATE, seed) as stats:
            retry_later = _ls_deliver(hooks)
        settle()
        with injectors.lemonsqueezy_api(world, countries) as api:
            counts = reconcile(older_than=timedelta(0))
        undelivered = _ls_deliver(retry_later)
        settle()
        violations = _ls_checks(world, undelivered)
    recovered = {k.removeprefix("ls_"): v for k, v in counts.items() if k.startswith("ls_")}
    return {"orders": len(world), "webhooks": len(hooks), **stats, "retried_later": len(retry_later),
            "recovered": recovered, "api_requests": api["requests"], "store_violations": violations}


SCENARIOS: dict[str, tuple[str, Callable[[random.Random, int, int], dict[str, Any]]]] = {
    "delivery-crash": ("30% of deliveries crash; at-least-once retries must still deliver every order", _delivery_crash),
    "duplicate-paid": ("every order.paid arrives twice; no order may get two keys", _duplicate_paid),
    "email-failures": ("30% of access emails fail to send; retries must still email every delivered order",
                       _email_failures),
    "phase1-exit": ("Phase 1 exit gate: 30% delivery and 30% email failures at once; retries and up to two "
                    "reconciler cycles must leave zero violations", _phase1_exit),
    "claude-down": ("the model API is down while a customer email arrives; it is answered after recovery", _claude_down),
    "ls-duplicate": ("every Lemon Squeezy webhook is delivered twice; nothing is mirrored or refunded twice",
                     _ls_duplicate),
    "ls-out-of-order": ("Lemon Squeezy refunds and licence keys arrive before their order; the mirror still ends "
                        "up matching the provider", _ls_out_of_order),
    "ls-lost": ("30% of Lemon Squeezy webhooks are lost; the reconciler's reconcile_recent recovers them from the "
                "API", _ls_lost),
}


def run_scenario(name: str, seed: int = 0, orders: int = 20) -> dict[str, Any]:
    """Run one chaos scenario on the configured DB; returns {scenario, seed, violations, stats}.
    Store-platform scenarios add their own checks (``stats["store_violations"]``) to the violations."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown chaos scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    with _chaos_run():
        stats = SCENARIOS[name][1](random.Random(seed), seed, orders)
    return {"scenario": name, "seed": seed, "violations": check_all() + stats.pop("store_violations", []),
            "stats": stats}
