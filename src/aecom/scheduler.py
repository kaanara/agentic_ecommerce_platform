"""Time-based triggers.

    uv run aecom scheduler [--demo]

--demo compresses time: one simulated market day per minute, so dashboards move while you watch.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.blocking import BlockingScheduler

import aecom.backup  # noqa: F401  (registers backup/verify job handlers and event subscribers)
import aecom.handlers  # noqa: F401  (registers event subscribers)
from aecom.bootstrap import init
from aecom import schedule_spec
from aecom.bus.events import drain, publish
from aecom.config import get_settings
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


def poll_telegram() -> None:
    from aecom.channels.telegram import poll_updates

    if (n := poll_updates()):
        log.info("telegram: %d update(s) processed", n)


def backup_job() -> None:
    if not get_settings().backup_enabled:
        return
    try:
        log.info("scheduled backup: %s", aecom.backup.run_backup_job({}))
    except Exception as exc:  # noqa: BLE001
        from aecom.observability import record_error

        record_error("scheduler", exc, context={"job": "backup"})


def run_jobs() -> None:
    from aecom import jobs

    if (ran := jobs.run_pending()):
        log.info("jobs: ran %s", ran)


def _schedule_jobs(sched: BlockingScheduler, demo: bool) -> None:
    """(Re)schedule every settings-driven job; safe to call again when the settings change."""
    s = get_settings()
    add = schedule_spec.add_job
    add(sched, drain, s.schedule_drain, "drain", "30s")
    add(sched, stock_check, s.schedule_stock_check, "stock_check", "60m")
    add(sched, reconcile_orders, s.schedule_reconcile, "reconcile", "15m")
    add(sched, monitors, s.schedule_monitors, "monitors", "5m")
    add(sched, provisioning_check, s.schedule_provisioning, "provisioning", "15m")
    add(sched, expire_approvals, s.schedule_expire_approvals, "expire_approvals", "30m")
    add(sched, sync_store_catalog, s.schedule_sync_store_catalog, "sync_store_catalog", "6h")
    add(sched, backup_job, s.backup_schedule, "backup", "cron:15 3 * * *")
    for job_id in ("poll_email", "poll_telegram"):
        if sched.get_job(job_id):
            sched.remove_job(job_id)
    if secrets.has("AGENTMAIL_API_KEY"):
        add(sched, poll_email, s.schedule_poll_email, "poll_email", "60s")
    if secrets.has("TELEGRAM_BOT_TOKEN") and s.telegram_enabled:
        add(sched, poll_telegram, s.schedule_poll_telegram, "poll_telegram", "10s")
    if demo:
        return
    add(sched, daily_finance_report, s.schedule_finance_daily, "finance_daily", "cron:0 7 * * *")
    add(sched, weekly_review, s.schedule_weekly_review, "weekly_review", "cron:0 8 * * 1")
    add(sched, weekly_review_report, s.schedule_weekly_review, "weekly_review_report", "cron:0 8 * * 1")
    add(sched, governance_report, s.schedule_governance_report, "governance_report", "cron:30 7 1 * *")


def build_scheduler(demo: bool = False) -> BlockingScheduler:
    """All jobs registered, not started (so the setup can be inspected)."""
    from aecom import settings_registry

    sched = BlockingScheduler()
    _schedule_jobs(sched, demo)
    sched.add_job(run_jobs, "interval", seconds=30, id="jobs")
    if demo:
        sched.add_job(simulate_market_day, "interval", minutes=1, id="market")
        sched.add_job(daily_finance_report, "interval", minutes=10, id="finance_daily")
        sched.add_job(weekly_review, "interval", minutes=60, id="weekly_review")
        sched.add_job(weekly_review_report, "interval", minutes=60, id="weekly_review_report")
        sched.add_job(governance_report, "interval", minutes=240, id="governance_report")

    seen = {"v": settings_registry.config_version()}

    def reload_settings() -> None:
        if settings_registry.refresh_if_changed() and settings_registry.config_version() != seen["v"]:
            seen["v"] = settings_registry.config_version()
            _schedule_jobs(sched, demo)
            log.info("scheduler: settings changed, jobs rescheduled")

    sched.add_job(reload_settings, "interval", seconds=30, id="reload_settings")
    return sched


def main(demo: bool = False) -> None:
    logging.basicConfig(level=logging.INFO)
    init()
    sched = build_scheduler(demo)
    sync_store_catalog()  # before any sale can arrive: a webhook for an unsynced product creates a placeholder (R16)
    log.info("scheduler started (demo=%s)", demo)
    sched.start()
