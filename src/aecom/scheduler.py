"""Time-based triggers.

    uv run aecom scheduler [--demo]

--demo compresses time: one simulated market day per minute, so dashboards move while you watch.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

import aecom.handlers  # noqa: F401  (registers event subscribers)
from aecom.bootstrap import init
from aecom.bus.events import drain, publish
from aecom.secrets import secrets
from aecom.store.adapter import get_store

log = logging.getLogger(__name__)


def stock_check() -> None:
    for item in get_store().inventory():
        if item["status"] == "active" and item["available_keys"] is not None and item["available_keys"] < 10:
            publish("inventory.low", {"sku": item["sku"], "available_keys": item["available_keys"]}, source="scheduler")
    drain()


def reconcile_orders() -> None:
    from aecom.reconciler import reconcile

    if (r := reconcile())["requeued"]:
        log.info("reconciler: %s", r)
    drain()


def sync_store_catalog() -> None:
    """Keep the local product table in step with the store platform (variants, prices, licence/download kind).
    Licence-key recovery only considers products of kind 'license', which only a sync sets (R16). Never raises."""
    from aecom.config import get_settings

    if get_settings().store_adapter != "lemonsqueezy" or not secrets.has("LEMONSQUEEZY_API_KEY"):
        return
    try:
        from aecom.store.adapter import get_store

        r = get_store().sync_catalog()
        log.info("catalog sync: %s", r)
    except Exception as exc:  # noqa: BLE001 - a store outage must not stop the scheduler
        from aecom.observability import record_error

        record_error("scheduler", exc, context={"job": "sync_store_catalog"})
        log.warning("catalog sync failed: %s", exc)


def poll_email() -> None:
    from aecom.integrations.agentmail import poll_inbox

    if (n := poll_inbox()):
        log.info("email: %d new inbound message(s)", n)
        drain()


def monitors() -> None:
    from aecom.monitors import run_monitors

    r = run_monitors()
    if r["failing"] or r["monitor_errors"] or r["alerted"]:
        log.info("monitors: %s", r)


def expire_approvals() -> int:
    """R8: mark pending approvals past their ``expires_at`` as expired even when nobody opens the dashboard."""
    from aecom import approvals

    n = approvals.expire_stale()
    log.info("approvals: %d expired", n)
    return n


def provisioning_check() -> None:
    """E1: activate vendors whose credential a human has put in the env, and hand them to the Foreman."""
    from aecom.provisioning import check_credentials

    if (r := check_credentials())["activated"]:
        log.info("provisioning: %s", r)
    drain()


def daily_finance_report() -> None:
    """J5: the daily flash (stored report + email). Finance only writes the commentary; nothing goes to shared memory."""
    from aecom.flash import send_daily_flash

    log.info("daily flash: %s", send_daily_flash())


def weekly_review() -> None:
    from aecom.graph.supervisor import run_company
    from aecom.scenarios import REVIEW_DIRECTIVE

    asyncio.run(run_company(REVIEW_DIRECTIVE))


def weekly_review_report() -> None:
    """J6: the weekly business review snapshot (code-computed, numbers-only text), emailed like the daily flash."""
    from aecom.reports import send_report

    log.info("weekly review report: %s", send_report("weekly_review", "last_7_days"))


def governance_report() -> None:
    """J12: last month's governance report (approvals, denies, controls, incidents, GDPR, provisioning)."""
    from aecom.reports import send_report

    log.info("governance report: %s", send_report("governance", "last_month"))


def simulate_market_day() -> None:
    from aecom.simulator import simulate_day

    log.info("market day: %s", simulate_day(support_emails=True))


def build_scheduler(demo: bool = False) -> BlockingScheduler:
    """All jobs registered, not started (so the setup can be inspected)."""
    sched = BlockingScheduler()
    sched.add_job(drain, "interval", seconds=30, id="drain")
    sched.add_job(stock_check, "interval", minutes=60, id="stock_check")
    sched.add_job(reconcile_orders, "interval", minutes=15, id="reconcile")
    sched.add_job(monitors, "interval", minutes=5, id="monitors")
    sched.add_job(provisioning_check, "interval", minutes=15, id="provisioning")
    sched.add_job(expire_approvals, "interval", minutes=30, id="expire_approvals")
    sched.add_job(sync_store_catalog, "interval", hours=6, id="sync_store_catalog")
    if secrets.has("AGENTMAIL_API_KEY"):
        sched.add_job(poll_email, "interval", seconds=60, id="poll_email")
    if demo:
        sched.add_job(simulate_market_day, "interval", minutes=1, id="market")
        sched.add_job(daily_finance_report, "interval", minutes=10, id="finance_daily")
        sched.add_job(weekly_review, "interval", minutes=60, id="weekly_review")
        sched.add_job(weekly_review_report, "interval", minutes=60, id="weekly_review_report")
        sched.add_job(governance_report, "interval", minutes=240, id="governance_report")
    else:
        sched.add_job(daily_finance_report, "cron", hour=7, id="finance_daily")
        sched.add_job(weekly_review, "cron", day_of_week="mon", hour=8, id="weekly_review")
        sched.add_job(weekly_review_report, "cron", day_of_week="mon", hour=8, id="weekly_review_report")
        sched.add_job(governance_report, "cron", day=1, hour=7, minute=30, id="governance_report")
    return sched


def main(demo: bool = False) -> None:
    logging.basicConfig(level=logging.INFO)
    init()
    sched = build_scheduler(demo)
    sync_store_catalog()  # before any sale can arrive: a webhook for an unsynced product creates a placeholder (R16)
    log.info("scheduler started (demo=%s)", demo)
    sched.start()
