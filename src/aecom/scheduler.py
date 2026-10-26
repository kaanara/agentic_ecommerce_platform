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


def daily_finance_report() -> None:
    """J5: the daily flash (stored report + email). Finance only writes the commentary; nothing goes to shared memory."""
    from aecom.flash import send_daily_flash

    log.info("daily flash: %s", send_daily_flash())


def weekly_review() -> None:
    from aecom.graph.supervisor import run_company
    from aecom.scenarios import REVIEW_DIRECTIVE

    asyncio.run(run_company(REVIEW_DIRECTIVE))


def simulate_market_day() -> None:
    from aecom.simulator import simulate_day

    log.info("market day: %s", simulate_day(support_emails=True))


def main(demo: bool = False) -> None:
    logging.basicConfig(level=logging.INFO)
    init()
    sched = BlockingScheduler()
    sched.add_job(drain, "interval", seconds=30, id="drain")
    sched.add_job(stock_check, "interval", minutes=60, id="stock_check")
    sched.add_job(reconcile_orders, "interval", minutes=15, id="reconcile")
    sched.add_job(monitors, "interval", minutes=5, id="monitors")
    if secrets.has("AGENTMAIL_API_KEY"):
        sched.add_job(poll_email, "interval", seconds=60, id="poll_email")
    if demo:
        sched.add_job(simulate_market_day, "interval", minutes=1, id="market")
        sched.add_job(daily_finance_report, "interval", minutes=10, id="finance_daily")
        sched.add_job(weekly_review, "interval", minutes=60, id="weekly_review")
    else:
        sched.add_job(daily_finance_report, "cron", hour=7, id="finance_daily")
        sched.add_job(weekly_review, "cron", day_of_week="mon", hour=8, id="weekly_review")
    log.info("scheduler started (demo=%s)", demo)
    sched.start()
