"""`aecom doctor`: one health report for the whole company, safe to paste into an issue (K6).

Every check returns ``{"name", "status": ok|warn|fail, "detail"}`` and is isolated: a check that raises is
reported as ``fail`` instead of stopping the report. Credentials are reported by NAME only, never by value.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from aecom.config import ROOT, get_settings

Check = dict[str, str]
OK, WARN, FAIL = "ok", "warn", "fail"
APPROVAL_MAX_AGE = timedelta(hours=24)
ERROR_WINDOW = timedelta(hours=24)
STUCK_RUN_AFTER = timedelta(minutes=30)
DISK_WARN_BYTES = 1024**3          # 1 GB
DISK_FAIL_BYTES = 200 * 1024**2    # 200 MB


def _c(name: str, status: str, detail: str) -> Check:
    return {"name": name, "status": status, "detail": detail}


def _sqlite_path() -> Path | None:
    url = get_settings().database_url
    return Path(url.removeprefix("sqlite:///")) if url.startswith("sqlite:///") else None


def _age(td: timedelta) -> str:
    hours = td.total_seconds() / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


# --- checks ------------------------------------------------------------------------------------------------

def check_database() -> Check:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from aecom.db import get_engine

    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
    path = _sqlite_path()
    where = str(path) if path else make_url(get_settings().database_url).render_as_string(hide_password=True)
    return _c("database", OK, f"reachable ({where})")


def check_schema() -> Check:
    from sqlalchemy import inspect

    from aecom.db import Base, get_engine

    insp = inspect(get_engine())
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            missing.append(table.name)
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        missing += [f"{table.name}.{col.name}" for col in table.columns if col.name not in existing]
    if missing:
        return _c("schema", FAIL, f"missing: {', '.join(missing)} (run `aecom init`)")
    return _c("schema", OK, f"all {len(Base.metadata.tables)} tables and their columns exist")


def check_kill_switch() -> Check:
    from aecom.company_settings import agents_paused_on, get_setting, kill_switch_on, safe_mode_on

    kill, paused, safe = kill_switch_on(), agents_paused_on(), safe_mode_on()
    detail = f"kill switch {'on' if kill else 'off'}, agents paused {'on' if paused else 'off'}, safe mode {'on' if safe else 'off'}"
    if safe:
        detail += f" ({get_setting('safe_mode_reason', '')})"
    return _c("kill_switch", WARN if kill or paused or safe else OK, detail)


def check_event_bus() -> Check:
    from aecom.bus.events import status

    st = status()
    detail = f"queued {st['queued']}, waiting for retry {st['waiting_for_retry']}, dead letter {st['dead_letter']}"
    if st["dead_letter"] > 0:
        return _c("event_bus", FAIL, detail + " (see `aecom events dead`)")
    return _c("event_bus", WARN if st["waiting_for_retry"] > 0 else OK, detail)


def check_invariants() -> Check:
    from collections import Counter

    from aecom.chaos.invariants import check_all

    counts = Counter(v["invariant"] for v in check_all())
    if counts:
        return _c("invariants", FAIL, "violated: " + ", ".join(f"{n} ({k})" for n, k in sorted(counts.items())))
    return _c("invariants", OK, "all business invariants hold")


def check_approvals() -> Check:
    from sqlalchemy import func, select

    from aecom.db import Approval, now, session

    with session() as s:
        count, oldest = s.execute(select(func.count(Approval.id), func.min(Approval.created_at))
                                  .where(Approval.status == "pending")).one()
    if not count:
        return _c("approvals", OK, "no pending approvals")
    age = now() - oldest
    detail = f"{count} pending, oldest {_age(age)}"
    return _c("approvals", WARN if age > APPROVAL_MAX_AGE else OK, detail)


def check_errors() -> Check:
    from sqlalchemy import func, select

    from aecom.db import ErrorRow, now, session

    with session() as s:
        n = s.scalar(select(func.count(ErrorRow.id)).where(ErrorRow.ts >= now() - ERROR_WINDOW)) or 0
    return _c("errors", WARN if n else OK, f"{n} error(s) in ops_errors in the last 24 h")


def check_stuck_runs() -> Check:
    from sqlalchemy import select

    from aecom.db import AgentRun, now, session

    with session() as s:
        ids = list(s.scalars(select(AgentRun.id).where(AgentRun.status == "running",
                                                       AgentRun.started_at < now() - STUCK_RUN_AFTER)
                             .order_by(AgentRun.started_at)))
    if ids:
        return _c("stuck_runs", FAIL, f"{len(ids)} agent run(s) running > 30 min: {', '.join(ids[:5])}")
    return _c("stuck_runs", OK, "no agent run running > 30 min")


def check_credentials() -> Check:
    """Names only: never put a credential value into the report."""
    from aecom.secrets import secrets

    settings = get_settings()
    present = {n: bool(secrets.get(n)) for n in
               ("ANTHROPIC_API_KEY", "AGENTMAIL_API_KEY", "AECOM_API_TOKEN", "DASHBOARD_PASSWORD_HASH")}
    problems = []
    if not present["ANTHROPIC_API_KEY"] and settings.llm_mode == "anthropic":
        problems.append("ANTHROPIC_API_KEY (LLM_MODE=anthropic)")
    if not present["AGENTMAIL_API_KEY"] and settings.agentmail_live:
        problems.append("AGENTMAIL_API_KEY (AGENTMAIL_LIVE=true)")
    from aecom.dashboard_auth import disabled

    problems += [n for n in ("AECOM_API_TOKEN", "DASHBOARD_PASSWORD_HASH") if not present[n]
                 and not (n == "DASHBOARD_PASSWORD_HASH" and disabled())]
    if disabled():
        problems.append("DASHBOARD_AUTH=off (dashboard login disabled)")
    detail = ", ".join(f"{n} {'present' if p else 'missing'}" for n, p in present.items())
    if problems:
        return _c("credentials", WARN, f"{detail}; needed: {', '.join(problems)}")
    return _c("credentials", OK, detail)


def check_agentmail_live() -> Check:
    live = get_settings().agentmail_live
    return _c("agentmail_live", OK, "on: emails are really sent" if live
              else "off: emails stay in the outbox (ops_outbox_emails)")


def check_disk() -> Check:
    path = _sqlite_path()
    if path is None:
        return _c("disk", OK, "not a local SQLite database; skipped")
    where = path.parent
    while not where.exists() and where != where.parent:
        where = where.parent
    free = shutil.disk_usage(where).free
    detail = f"{free / 1024**3:.1f} GB free at {where}"
    if free < DISK_FAIL_BYTES:
        return _c("disk", FAIL, detail)
    return _c("disk", WARN if free < DISK_WARN_BYTES else OK, detail)


BACKUP_WARN_AGE = timedelta(hours=26)
BACKUP_FAIL_AGE = timedelta(hours=72)


def check_backup() -> Check:
    from datetime import UTC, datetime

    from sqlalchemy import func, select

    from aecom import backup
    from aecom.db import Order, session

    with session() as s:
        has_orders = (s.scalar(select(func.count()).select_from(Order)) or 0) > 0
    backups = backup.list_backups()
    if not backups:
        if has_orders:
            return _c("backup", WARN, "no backups yet but orders exist: run `aecom backup` (or enable the timer)")
        return _c("backup", OK, "no data yet, nothing to back up")
    newest = backups[0]
    problems = backup.check_hashes(newest)
    if problems:
        return _c("backup", FAIL, f"newest backup {newest.name} is damaged: {'; '.join(problems)}")
    age = datetime.now(UTC) - backup.created_at(newest)
    detail = f"newest backup {newest.name} is {_age(age)} old ({len(backups)} kept)"
    status = FAIL if age > BACKUP_FAIL_AGE else WARN if age > BACKUP_WARN_AGE else OK
    if status != OK:
        detail += " (expected at least daily)"
    if has_orders and not get_settings().backup_target:
        detail += "; backups are only on this device (set BACKUP_TARGET)"
        status = WARN if status == OK else status
    return _c("backup", status, detail)


def check_catalog_sync() -> Check:
    """With Lemon Squeezy as the store, the local catalog must have been synced at least once (R16)."""
    from sqlalchemy import func, select

    from aecom.db import Product, session

    if get_settings().store_adapter != "lemonsqueezy":
        return _c("catalog_sync", OK, f"not applicable (store adapter: {get_settings().store_adapter})")
    with session() as s:
        n = s.scalar(select(func.count(Product.id)).where(Product.external_id.is_not(None), Product.kind != "download")) or 0
        total = s.scalar(select(func.count(Product.id)).where(Product.external_id.is_not(None))) or 0
    if total == 0:
        return _c("catalog_sync", WARN, "no store products yet: run `aecom store sync` before the first sale")
    return _c("catalog_sync", OK, f"{total} store product(s) known, {n} licence/subscription")


CHECKS: list[tuple[str, Callable[[], Check]]] = [
    ("database", check_database),
    ("schema", check_schema),
    ("kill_switch", check_kill_switch),
    ("event_bus", check_event_bus),
    ("invariants", check_invariants),
    ("approvals", check_approvals),
    ("errors", check_errors),
    ("stuck_runs", check_stuck_runs),
    ("credentials", check_credentials),
    ("agentmail_live", check_agentmail_live),
    ("catalog_sync", check_catalog_sync),
    ("disk", check_disk),
    ("backup", check_backup),
]


def run_checks() -> list[Check]:
    """Run every check; one that raises is reported as ``fail`` and the others still run."""
    out = []
    for name, fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001 - the doctor must never crash
            first_line = (str(e).splitlines() or [""])[0][:200]
            out.append(_c(name, FAIL, f"check crashed: {type(e).__name__}: {first_line}"))
    return out


def version() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return f"git {r.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("aecom")
    except Exception:  # noqa: BLE001
        return "unknown"


def report(checks: list[Check] | None = None) -> dict[str, Any]:
    from aecom.db import now

    checks = run_checks() if checks is None else checks
    summary = {s: sum(c["status"] == s for c in checks) for s in (OK, WARN, FAIL)}
    return {"version": version(), "generated_at": f"{now():%Y-%m-%d %H:%M:%S} UTC", "summary": summary,
            "checks": checks}


def format_report(rep: dict[str, Any]) -> str:
    width = max((len(c["name"]) for c in rep["checks"]), default=0)
    lines = [f"aecom doctor - {rep['version']} - {rep['generated_at']}", ""]
    lines += [f"[{c['status'].upper():<4}] {c['name']:<{width}}  {c['detail']}" for c in rep["checks"]]
    s = rep["summary"]
    lines += ["", f"{s[OK]} ok, {s[WARN]} warn, {s[FAIL]} fail"]
    return "\n".join(lines)
