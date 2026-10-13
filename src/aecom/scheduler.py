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
from aecom.store.adapter import get_store

log = logging.getLogger(__name__)


def stock_check() -> None:
    for item in get_store().inventory():
        if item["status"] == "active" and item["available_keys"] is not None and item["available_keys"] < 10:
            publish("inventory.low", {"sku": item["sku"], "available_keys": item["available_keys"]}, source="scheduler")
    drain()


def daily_finance_report() -> None:
    from aecom.graph.agents import run_agent

    r = asyncio.run(run_agent("finance", "Daily report: P&L, KPIs vs OKRs, campaign ROAS, AI cost per order. "
                                         "Store the 3 most important insights with remember(shared=true)."))
    log.info("finance report: %s", r.output[:300])


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
    if demo:
        sched.add_job(simulate_market_day, "interval", minutes=1, id="market")
        sched.add_job(daily_finance_report, "interval", minutes=10, id="finance_daily")
        sched.add_job(weekly_review, "interval", minutes=60, id="weekly_review")
    else:
        sched.add_job(daily_finance_report, "cron", hour=7, id="finance_daily")
        sched.add_job(weekly_review, "cron", day_of_week="mon", hour=8, id="weekly_review")
    log.info("scheduler started (demo=%s)", demo)
    sched.start()
