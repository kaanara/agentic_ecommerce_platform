"""Roadmap L4: launch readiness for the first real product.

The roadmap's next milestone is "launch one product and get about 10 real sales, with the owner approving almost
everything" (``docs/wiki/Roadmap.md``); autonomy promotion (``docs/wiki/Autonomy.md``) stays locked at stage 1 until
then. ``checks()`` is a read-only checklist computed from the real state of the company (dashboard login, backups,
credentials, the store adapter, alerting, the kill switch/safe mode, ``aecom doctor`` and the migration head) -
nothing here changes anything, and a check that raises is reported as ``warn`` instead of breaking the whole page
(same contract as ``aecom.doctor.run_checks``).

A handful of items (the Anthropic spend cap, the legal pages, human on-call, the webhook being publicly reachable,
and the data retention table) cannot be verified by the app at all - only a human can judge them. Those are ticked
with ``acknowledge(key, by)``, which is audited and has no agent tool, the same way ``aecom.periods.close/reopen``
are human-only (R19-style: see ``test_launch.py`` for the "no agent tool" assertion).

``progress()`` reports how close the company is to the ~10 real sales that unlock autonomy promotion, reusing
``aecom.trust.real_sales()``/``thresholds()`` (the same evidence ``trust.eligibility`` checks) and
``aecom.monitors.real_net_revenue_eur()`` (the same real-sales-only revenue the Pi-to-cloud migration reminder
uses). ``ready()`` is True only once every check is no longer ``todo`` (a ``warn`` is a heads-up, not a blocker).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Callable, NamedTuple

from aecom.company_settings import get_setting, kill_switch_on, safe_mode_on, set_flag
from aecom.config import get_settings

OK, TODO, WARN = "ok", "todo", "warn"

BACKUP_VERIFIED_MAX_AGE = timedelta(hours=26)

# The five human-only acknowledgements the app cannot verify itself: (title, where to look before ticking it).
ACK_ITEMS: dict[str, tuple[str, str]] = {
    "spend_cap": ("Anthropic spend cap acknowledged",
                 "Set a spend cap at console.anthropic.com, then tick this once it is set."),
    "legal": ("Legal pages acknowledged",
             "Publish the legal pages (docs/legal/: privacy policy, terms, imprint), then tick this."),
    "oncall": ("Human on-call acknowledged",
              "Make sure a human is reachable for P1/P2 alerts (see docs/wiki/Runbooks.md), then tick this."),
    "webhook": ("Webhook URL is publicly reachable (acknowledged)",
               "Set up and test the tunnel (docs/wiki/Raspberry-Pi.md, section 6, Webhooks), then tick this."),
    "retention": ("Data retention table acknowledged",
                 "Decide the open [DECISION] rows in docs/wiki/GDPR-and-Retention.md (H10), then tick this."),
}


class Check(NamedTuple):
    id: str
    title: str
    status: str  # ok | todo | warn
    detail: str
    where_to_fix: str


def _c(id: str, title: str, status: str, detail: str, where_to_fix: str) -> Check:
    return Check(id, title, status, detail, where_to_fix)


def _ack(key: str) -> bool:
    return get_setting(f"launch_ack_{key}", "off") == "on"


def acknowledge(key: str, by: str) -> None:
    """A human ticks one of ``ACK_ITEMS`` as done. Audited as ``launch_ack_<key>``; no agent tool exists for this
    (``test_launch.py`` asserts the catalog and policy stay free of it), the same way period close/reopen are
    human-only (``aecom.periods``)."""
    if key not in ACK_ITEMS:
        raise ValueError(f"unknown launch acknowledgement: {key!r}")
    if not (by or "").strip():
        raise ValueError("acknowledging a launch item needs the name of the human (by)")
    set_flag(f"launch_ack_{key}", True, by)


# --- individual checks -------------------------------------------------------------------------------------

def _check_dashboard_login() -> Check:
    from aecom import dashboard_auth

    ok = not dashboard_auth.disabled() and dashboard_auth.is_configured()
    if dashboard_auth.disabled():
        detail = "dashboard login is off (DASHBOARD_AUTH=off)"
    elif not dashboard_auth.is_configured():
        detail = "no dashboard password is set yet"
    else:
        detail = "on"
    return _c("dashboard_login", "Dashboard login is on", OK if ok else TODO, detail,
              "Settings > Security and backups > Security")


def _check_backup_scheduled() -> Check:
    settings = get_settings()
    ok = settings.backup_enabled
    detail = f"backups {'are' if ok else 'are not'} scheduled ({settings.backup_schedule})"
    return _c("backup_scheduled", "Backups are scheduled", OK if ok else TODO, detail,
              "Settings > Security and backups > Backups and updates")


def _verified_backup_times() -> list[datetime]:
    """``verified_at`` of every backup whose manifest was marked verified (``aecom.backup.update_manifest``)."""
    from aecom import backup

    out: list[datetime] = []
    for path in backup.list_backups():
        try:
            manifest = backup.read_manifest(path)
        except Exception:  # noqa: BLE001 - an unreadable manifest just doesn't count as evidence
            continue
        if not manifest.get("verified"):
            continue
        ts = manifest.get("verified_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        out.append(dt if dt.tzinfo else dt.replace(tzinfo=UTC))
    return out


def _check_backup_verified_recent() -> Check:
    where = "Settings > Security and backups > Backups and updates"
    times = _verified_backup_times()
    if not times:
        return _c("backup_verified_recent", "A backup was verified in the last 26 hours", TODO,
                  "no verified backup yet: run `aecom backup` then `aecom backup verify`", where)
    age = datetime.now(UTC) - max(times)
    hours = age.total_seconds() / 3600
    ok = age <= BACKUP_VERIFIED_MAX_AGE
    return _c("backup_verified_recent", "A backup was verified in the last 26 hours", OK if ok else TODO,
              f"newest verified backup is {hours:.1f} h old", where)


def _check_backup_offdevice() -> Check:
    target = get_settings().backup_target
    ok = bool(target)
    detail = f"off-device target set ({target})" if ok else "no BACKUP_TARGET set: backups stay on this device"
    return _c("backup_offdevice", "An off-device backup target is set", OK if ok else TODO, detail,
              "Settings > Security and backups > Backups and updates")


def _check_backup_passphrase() -> Check:
    from aecom.secrets import secrets

    ok = bool((secrets.get("BACKUP_PASSPHRASE") or "").strip())
    detail = "encryption passphrase set" if ok else "no BACKUP_PASSPHRASE set: backups are stored unencrypted"
    return _c("backup_passphrase", "Backup encryption passphrase is set", OK if ok else TODO, detail,
              "Settings > Security and backups > Backups and updates")


def _check_anthropic() -> Check:
    from aecom.secrets import secrets

    settings = get_settings()
    has_key = bool(secrets.get("ANTHROPIC_API_KEY"))
    is_anthropic = settings.llm_mode == "anthropic"
    ok = has_key and is_anthropic
    missing = [m for cond, m in ((not has_key, "ANTHROPIC_API_KEY not set"),
                                (not is_anthropic, f"LLM_MODE={settings.llm_mode!r}, not 'anthropic'")) if cond]
    detail = "key set, LLM_MODE=anthropic" if ok else "; ".join(missing)
    return _c("anthropic_llm", "Anthropic key set and LLM_MODE=anthropic", OK if ok else TODO, detail,
              "Settings > Money and AI > AI and models")


def _check_store_ready() -> Check:
    from sqlalchemy import func, select

    from aecom.db import Product, session
    from aecom.secrets import secrets

    settings = get_settings()
    is_ls = settings.store_adapter == "lemonsqueezy"
    has_key = bool(secrets.get("LEMONSQUEEZY_API_KEY"))
    has_secret = bool(secrets.get("LEMONSQUEEZY_WEBHOOK_SECRET"))
    with session() as s:
        synced = (s.scalar(select(func.count(Product.id)).where(Product.external_id.is_not(None))) or 0) > 0
    ok = is_ls and has_key and has_secret and synced
    missing = []
    if not is_ls:
        missing.append(f"STORE_ADAPTER={settings.store_adapter!r}, not 'lemonsqueezy'")
    if is_ls and not has_key:
        missing.append("LEMONSQUEEZY_API_KEY not set")
    if is_ls and not has_secret:
        missing.append("LEMONSQUEEZY_WEBHOOK_SECRET not set")
    if is_ls and has_key and has_secret and not synced:
        missing.append("no synced store products yet: run `aecom store sync`")
    detail = "lemonsqueezy configured, keys set, catalog synced" if ok else "; ".join(missing)
    return _c("store_ready", "Store adapter is the real store (Lemon Squeezy), with keys and a synced catalog",
              OK if ok else TODO, detail, "Settings > Money and AI > Store and email")


def _check_agentmail() -> Check:
    from aecom.secrets import secrets

    settings = get_settings()
    has_key = bool(secrets.get("AGENTMAIL_API_KEY"))
    inbox = (settings.agentmail_org_inbox or "").strip()
    ok = settings.agentmail_live and has_key and bool(inbox)
    missing = []
    if not settings.agentmail_live:
        missing.append("AGENTMAIL_LIVE=false: emails stay in the outbox")
    if not has_key:
        missing.append("AGENTMAIL_API_KEY not set")
    if not inbox:
        missing.append("no inbox configured (AGENTMAIL_ORG_INBOX)")
    detail = f"live, inbox {inbox}" if ok else "; ".join(missing)
    return _c("agentmail_live", "AgentMail is live, with an inbox", OK if ok else TODO, detail,
              "Settings > Money and AI > Store and email")


def _check_alerts_owner() -> Check:
    from aecom.channels.router import active_links
    from aecom.secrets import secrets

    telegram = bool(active_links("telegram"))
    ntfy = bool(secrets.get("NTFY_URL"))
    ok = telegram or ntfy
    parts = [p for cond, p in ((telegram, "Telegram paired"), (ntfy, "ntfy URL set")) if cond]
    detail = ", ".join(parts) if parts else "no Telegram pairing and no NTFY_URL: P1/P2 alerts reach nobody"
    return _c("alerts_owner", "Alerts reach the owner (Telegram paired or ntfy URL set)", OK if ok else TODO, detail,
              "Settings > Channels and chat > Channels (Telegram), or Alerts and notifications (ntfy URL)")


def _check_alerts_heartbeat() -> Check:
    from aecom.secrets import secrets

    ok = bool(secrets.get("HEALTHCHECKS_PING_URL"))
    detail = "healthchecks ping URL set" if ok else "no HEALTHCHECKS_PING_URL set: a silent crash would go unnoticed"
    return _c("alerts_heartbeat", "A healthchecks ping URL is set", OK if ok else TODO, detail,
              "Settings > Channels and chat > Alerts and notifications")


def _check_ack(key: str) -> Check:
    title, hint = ACK_ITEMS[key]
    on = _ack(key)
    detail = "acknowledged by a human" if on else "not yet acknowledged - the app cannot verify this one itself"
    return _c(f"ack_{key}", title, OK if on else TODO, detail, hint)


def _check_kill_switch_safe_mode() -> Check:
    kill, safe = kill_switch_on(), safe_mode_on()
    ok = not kill and not safe
    detail = f"kill switch {'on' if kill else 'off'}, safe mode {'on' if safe else 'off'}"
    return _c("kill_switch_safe_mode", "Kill switch is off and safe mode is off", OK if ok else TODO, detail,
              "Overview")


def _check_doctor() -> Check:
    from aecom import doctor

    rows = doctor.run_checks()
    fails = [r["name"] for r in rows if r["status"] == doctor.FAIL]
    warns = [r["name"] for r in rows if r["status"] == doctor.WARN]
    if fails:
        return _c("doctor_fail", "`aecom doctor` has no FAIL", TODO, f"FAIL: {', '.join(fails)}", "Health > Doctor")
    if warns:
        return _c("doctor_fail", "`aecom doctor` has no FAIL", WARN, f"no FAIL; WARN: {', '.join(warns)}",
                  "Health > Doctor")
    return _c("doctor_fail", "`aecom doctor` has no FAIL", OK, "all checks ok", "Health > Doctor")


def _check_migrations() -> Check:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from aecom.db import _alembic_config, get_engine

    cfg = _alembic_config()
    heads = set(ScriptDirectory.from_config(cfg).get_heads())
    with get_engine().connect() as conn:
        current = set(MigrationContext.configure(conn).get_current_heads())
    where = "run `aecom init`"
    if not current:
        return _c("migrations_head", "Migrations are at head", TODO, "database not yet versioned", where)
    if current != heads:
        return _c("migrations_head", "Migrations are at head", TODO,
                  f"current {sorted(current)} != head {sorted(heads)}", where)
    return _c("migrations_head", "Migrations are at head", OK, f"at head ({', '.join(sorted(heads))})", where)


CHECKS: list[Callable[[], Check]] = [
    _check_dashboard_login,
    _check_backup_scheduled,
    _check_backup_verified_recent,
    _check_backup_offdevice,
    _check_backup_passphrase,
    _check_anthropic,
    _check_store_ready,
    _check_agentmail,
    _check_alerts_owner,
    _check_alerts_heartbeat,
    partial(_check_ack, "spend_cap"),
    partial(_check_ack, "legal"),
    partial(_check_ack, "oncall"),
    _check_kill_switch_safe_mode,
    _check_doctor,
    _check_migrations,
    partial(_check_ack, "webhook"),
    partial(_check_ack, "retention"),
]


def checks() -> list[Check]:
    """Run every check; one that raises is reported as ``warn`` (not ``todo``: an unknown state should not by
    itself block launch) and the others still run, mirroring ``aecom.doctor.run_checks``."""
    out: list[Check] = []
    for fn in CHECKS:
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001 - the launch checklist must never crash the page
            name = fn.func.__name__ if isinstance(fn, partial) else fn.__name__
            first_line = (str(e).splitlines() or [""])[0][:200]
            out.append(_c(name.removeprefix("_check_"), name, WARN, f"check crashed: {type(e).__name__}: {first_line}", ""))
    return out


def ready(rows: list[Check] | None = None) -> bool:
    """True once nothing is still ``todo`` (a ``warn`` is a heads-up, not a blocker)."""
    rows = checks() if rows is None else rows
    return all(c.status != TODO for c in rows)


def progress() -> dict[str, Any]:
    """Real sales toward the ~10-sale threshold that unlocks autonomy promotion, plus net revenue from those same
    real sales and a short stage label for the roadmap's "first 10 sales" milestone."""
    from aecom import trust
    from aecom.monitors import real_net_revenue_eur

    sales = trust.real_sales()
    threshold = int(trust.thresholds()["AUTONOMY_MIN_REAL_SALES"])
    revenue = real_net_revenue_eur()
    remaining = max(threshold - sales, 0)
    fraction = min(sales / threshold, 1.0) if threshold else 1.0
    if sales <= 0:
        stage_text = f"Stage: launch and get your first real sale (0 of {threshold})."
    elif remaining:
        stage_text = f"Stage: first {threshold} real sales ({sales} of {threshold}, {remaining} to go)."
    else:
        stage_text = (f"Stage: first {threshold} real sales reached ({sales} of {threshold}) - "
                      "autonomy promotion can now be considered (see Trust).")
    return {"real_sales": sales, "threshold": threshold, "remaining": remaining, "fraction": fraction,
            "revenue_eur": revenue, "stage_text": stage_text}
