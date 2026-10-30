"""One registry of every option the owner can change in the dashboard (Settings page).

Layers for non-secret config (``storage="config"``, the fields of ``config.Settings``):
    database override (ops_settings ``config:<name>``)  >  .env / environment  >  built-in default.
Secrets and plain .env values (``storage="secret"`` / ``"env"``) are written to ``.env`` by ``envfile`` and never to the
database (backups contain the database but deliberately not .env); secret values are never returned or displayed.
``storage="db"`` keys live in ops_settings under their own name, ``"managed"`` ones have their own page/service
(login, kill switch, autonomy stages) and ``"readonly"`` ones are shown with an explanation only.

Changes bump ``config_version``; every process calls ``refresh_if_changed()`` on its own tick (scheduler 30 s, API per
request, dashboard per rerun) to pick them up. Overrides are applied IN PLACE on the ``get_settings()`` singleton.
Agents have no tool that reaches this module (a test asserts it).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from aecom import envfile
from aecom.audit import audit
from aecom.config import Settings, get_settings
from aecom.db import AuditLog, Setting, now, session
from aecom.schedule_spec import is_valid as is_valid_schedule
from aecom.secrets import secrets

CONFIG_PREFIX = "config:"
VERSION_KEY = "config_version"

SECTIONS = ("AI and models", "Budgets and costs", "Store and email", "Alerts and notifications", "Channels",
            "Chat", "Schedules", "Security", "Backups and updates", "Autonomy", "Advanced")


@dataclass(frozen=True)
class Field:
    key: str  # lowercase; the Settings attribute, or the lowercase env name. The env name is key.upper().
    section: str
    label: str
    help: str = ""
    kind: str = "str"  # bool | int | float | str | enum | list | pair | schedule | time | secret | path
    storage: str = "config"  # config | env | secret | db | managed | readonly
    apply: str = "live"  # live | reload | restart
    risk: str = "normal"  # normal | sensitive | critical (critical = password again before saving)
    choices: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None
    link: str = ""  # page that owns a managed value

    @property
    def env(self) -> str:
        return self.key.upper()


def _f(key: str, section: str, label: str, help: str = "", kind: str = "str", **kw: Any) -> Field:
    return Field(key, section, label, help, kind, **kw)


def _secret(key: str, section: str, label: str, help: str = "", **kw: Any) -> Field:
    kw.setdefault("risk", "critical")
    return Field(key, section, label, help, "secret", "secret", **kw)


_SCHED = "Interval (30s, 15m, 6h) or cron:MIN HOUR DAY MONTH WEEKDAY, e.g. cron:0 7 * * *"

_FIELDS: list[Field] = [
    # --- AI and models
    _secret("anthropic_api_key", "AI and models", "Anthropic API key", "Used by every agent. Also set a spend cap in the Anthropic console."),
    _f("llm_mode", "AI and models", "LLM mode", "anthropic = real models, scripted = offline test model.", "enum",
       choices=("anthropic", "scripted"), risk="critical"),
    _f("model_opus", "AI and models", "Opus model id"), _f("model_sonnet", "AI and models", "Sonnet model id"),
    _f("model_haiku", "AI and models", "Haiku model id"),
    _f("agent_max_steps", "AI and models", "Max steps per agent run", kind="int", min=1, max=50),
    _f("price_opus", "AI and models", "Opus price (USD per million tokens: input, output)", kind="pair"),
    _f("price_sonnet", "AI and models", "Sonnet price (input, output)", kind="pair"),
    _f("price_haiku", "AI and models", "Haiku price (input, output)", kind="pair"),
    _f("usd_to_eur", "AI and models", "USD to EUR rate", kind="float", min=0.1, max=10),
    _f("web_search_tool_type", "AI and models", "Web search tool version"),
    _f("web_search_max_uses", "AI and models", "Web searches per API call", kind="int", min=0, max=50),
    _f("web_search_max_per_run", "AI and models", "Web searches per run", kind="int", min=0, max=200),
    _f("web_search_price_eur", "AI and models", "Web search price (EUR each)", kind="float", min=0, max=1),
    _f("web_search_allowed_domains", "AI and models", "Web search: allowed domains", "Allowed wins when both lists are set.", "list"),
    _f("web_search_blocked_domains", "AI and models", "Web search: blocked domains", kind="list"),
    _f("web_fetch_tool_type", "AI and models", "Web fetch tool version"),
    _f("web_fetch_max_uses", "AI and models", "Web fetches per API call", kind="int", min=0, max=50),
    _f("web_fetch_max_content_tokens", "AI and models", "Web fetch: max tokens per page", kind="int", min=100, max=100000),
    _f("web_fetch_price_eur", "AI and models", "Web fetch price (EUR each)", kind="float", min=0, max=1),
    _f("web_fetch_allowed_domains", "AI and models", "Web fetch: allowed domains", kind="list"),
    _f("web_fetch_blocked_domains", "AI and models", "Web fetch: blocked domains", kind="list"),
    _f("mcp_autoload", "AI and models", "Load registry MCP servers into agents", kind="bool", apply="restart"),
    _f("mcp_load_timeout_s", "AI and models", "MCP load timeout (s)", kind="float", min=1, max=300, apply="restart"),
    _secret("langsmith_api_key", "AI and models", "LangSmith API key", "Keep tracing off until the legal review (customer data in traces)."),
    _f("langsmith_tracing", "AI and models", "LangSmith tracing", kind="bool", storage="env", apply="restart", risk="sensitive"),
    _f("langsmith_project", "AI and models", "LangSmith project", storage="env", apply="restart"),
    _secret("sentry_dsn", "AI and models", "Sentry DSN", "Forwards unexpected errors to Sentry (needs the observability extra)."),
    # --- Budgets and costs
    _f("marketing_budget_month_eur", "Budgets and costs", "Monthly marketing budget (EUR)", "Campaign spend above this escalates.",
       "float", storage="db", min=0, max=1_000_000, risk="sensitive"),
    _f("migration_revenue_eur", "Budgets and costs", "Revenue that triggers the cloud-migration reminder (EUR)", kind="float",
       storage="env", min=0, max=1_000_000),
    # --- Store and email
    _f("store_adapter", "Store and email", "Store adapter", "mock until the real store is provisioned.", "enum",
       choices=("mock", "lemonsqueezy"), risk="critical"),
    _secret("lemonsqueezy_api_key", "Store and email", "Lemon Squeezy API key"),
    _secret("lemonsqueezy_webhook_secret", "Store and email", "Lemon Squeezy webhook secret"),
    _f("lemonsqueezy_store_id", "Store and email", "Lemon Squeezy store id", risk="sensitive"),
    _f("lemonsqueezy_base_url", "Store and email", "Lemon Squeezy API URL", risk="sensitive"),
    _f("download_route_enabled", "Store and email", "Customer download route",
       "Serves GET /download/{order_id}?token=... for files stored on this machine. Needs a public URL.", "bool", risk="sensitive"),
    _f("datev_berater_nr", "Store and email", "DATEV consultant number", "From your accountant. Used by the accountant export."),
    _f("datev_mandant_nr", "Store and email", "DATEV client number", "From your accountant. Used by the accountant export."),
    _f("datev_wj_beginn", "Store and email", "Financial year start (YYYYMMDD)", "Empty = 1 January of the exported year."),
    _f("downloads_dir", "Store and email", "Downloads folder", "Files must live inside this folder.", "path", storage="readonly"),
    _f("lemonsqueezy_currency", "Store and email", "Store currency", "Orders in another currency are refused, not converted."),
    _f("lemonsqueezy_timeout_s", "Store and email", "Lemon Squeezy timeout (s)", kind="float", min=1, max=120),
    _f("license_key_source", "Store and email", "Licence key source", "mock mints fake keys (demo only); none = manual keys only.",
       "enum", choices=("mock", "none"), risk="sensitive"),
    _f("download_base_url", "Store and email", "Download base URL", risk="sensitive"),
    _f("download_link_days", "Store and email", "Download link validity (days)", kind="int", min=1, max=365),
    _secret("agentmail_api_key", "Store and email", "AgentMail API key", "Use the inbox-scoped key, not an org-wide one."),
    _secret("agentmail_webhook_secret", "Store and email", "AgentMail webhook secret"),
    _f("agentmail_base_url", "Store and email", "AgentMail API URL", risk="sensitive"),
    _f("agentmail_org_inbox", "Store and email", "AgentMail organisation inbox"),
    _f("agentmail_live", "Store and email", "Send real emails", "Off = emails stay in the local outbox.", "bool", risk="critical"),
    _secret("google_ads_refresh_token", "Store and email", "Google Ads refresh token"),
    # --- Alerts and notifications
    _secret("ntfy_url", "Alerts and notifications", "ntfy topic URL", "The topic name is effectively a secret."),
    _secret("ntfy_token", "Alerts and notifications", "ntfy access token"),
    _f("alert_email", "Alerts and notifications", "Alert email (P1/P2)", storage="env"),
    _f("report_email", "Alerts and notifications", "Daily report email", storage="env"),
    _secret("healthchecks_ping_url", "Alerts and notifications", "healthchecks.io ping URL"),
    _f("quiet_hours_start", "Alerts and notifications", "Quiet hours start (HH:MM)", "Agents' messages wait; P1 alerts ignore this.", "time"),
    _f("quiet_hours_end", "Alerts and notifications", "Quiet hours end (HH:MM)", kind="time"),
    _f("owner_message_daily_limit", "Alerts and notifications", "Messages per agent per day", kind="int", min=0, max=100),
    # --- Channels
    _f("telegram_enabled", "Channels", "Telegram enabled", kind="bool", risk="sensitive"),
    _secret("telegram_bot_token", "Channels", "Telegram bot token", "From @BotFather."),
    _f("whatsapp_enabled", "Channels", "WhatsApp enabled", "Needs a Meta business account and a public webhook.", "bool", risk="sensitive"),
    _secret("whatsapp_token", "Channels", "WhatsApp access token"),
    _secret("whatsapp_phone_id", "Channels", "WhatsApp phone number id"),
    _secret("whatsapp_app_secret", "Channels", "WhatsApp app secret"),
    _secret("whatsapp_verify_token", "Channels", "WhatsApp verify token"),
    # --- Chat
    _f("chat_agents", "Chat", "Agents available for chat", kind="list", risk="sensitive"),
    _f("chat_history_turns", "Chat", "Turns kept in context", kind="int", min=2, max=100),
    _f("chat_daily_cap_share", "Chat", "Share of the daily budget chat may use", kind="float", min=0, max=1),
    _f("chat_retention_days", "Chat", "Delete conversations idle for (days, 0 = keep)", kind="int", min=0, max=3650),
    _f("chat_max_attachment_mb", "Chat", "Max attachment size (MB)", kind="int", min=1, max=50),
    # --- Schedules
    *[_f(f"schedule_{name}", "Schedules", label, _SCHED, "schedule", apply="live")
      for name, label in (("drain", "Event bus drain"), ("stock_check", "Stock check"), ("reconcile", "Order reconciler"),
                          ("monitors", "Monitors"), ("provisioning", "Provisioning check"),
                          ("expire_approvals", "Expire stale approvals"), ("sync_store_catalog", "Store catalog sync"),
                          ("poll_email", "Email poll"), ("poll_telegram", "Telegram poll"), ("finance_daily", "Daily finance report"),
                          ("weekly_review", "Weekly review"), ("governance_report", "Monthly governance report"))],
    # --- Security
    _f("dashboard_idle_hours", "Security", "Sign out after idle (hours)", kind="float", min=0.1, max=720),
    _f("dashboard_auth", "Security", "Dashboard login", "Turn on, change the password or turn off in Security.", "bool",
       storage="managed", link="Security", risk="critical"),
    _f("dashboard_password_hash", "Security", "Dashboard password", kind="secret", storage="managed", link="Security", risk="critical"),
    _secret("aecom_api_token", "Security", "API token", "Bearer token for every API route except /health and signed webhooks."),
    # --- Backups and updates
    _f("backup_enabled", "Backups and updates", "Scheduled backups", kind="bool", risk="sensitive"),
    _f("backup_schedule", "Backups and updates", "Backup schedule", _SCHED, "schedule"),
    _f("backup_verify_after", "Backups and updates", "Verify after every backup", kind="bool"),
    _f("backup_keep_daily", "Backups and updates", "Keep newest daily backups", kind="int", min=1, max=365),
    _f("backup_keep_weeks", "Backups and updates", "Keep one per ISO week, for (weeks)", kind="int", min=0, max=52),
    _f("backup_keep_labelled", "Backups and updates", "Keep newest labelled backups", kind="int", min=0, max=50),
    _f("backup_target", "Backups and updates", "Off-device copy target", "A local path or user@host:/path (rsync). Backups hold customer data: use an encrypted target.", risk="sensitive"),
    _secret("backup_passphrase", "Backups and updates", "Backup encryption passphrase",
            "Encrypts new backups at rest (AES-256-GCM, at least 12 characters). Keep it in a password manager: without "
            "it encrypted backups cannot be restored. Set it before the first backup you rely on; older backups stay plain."),
    _f("backup_dir", "Backups and updates", "Backup directory", "Changing it from the running app is not offered.", "path", storage="readonly"),
    # --- Autonomy (evidence gate: lowering these weakens it)
    _f("autonomy_min_real_sales", "Autonomy", "Real sales needed before a promotion", kind="float", storage="env", min=0, risk="critical", link="Trust"),
    _f("autonomy_min_decisions", "Autonomy", "Human decisions needed before a promotion", kind="float", storage="env", min=0, risk="critical", link="Trust"),
    _f("autonomy_min_unchanged_rate", "Autonomy", "Approve-unchanged rate needed", kind="float", storage="env", min=0, max=1, risk="critical", link="Trust"),
    _f("autonomy_cooldown_days", "Autonomy", "Cooldown after a demotion (days)", kind="float", storage="env", min=0, risk="critical", link="Trust"),
    # --- Advanced (read-only: changing these from inside the running app would cut it off from its own data)
    _f("database_url", "Advanced", "Database URL", "Set in .env; changing it needs a restart and a data move.", "path", storage="readonly"),
    _f("agents_dir", "Advanced", "Agents directory", kind="path", storage="readonly"),
    _f("company_dir", "Advanced", "Company directory", kind="path", storage="readonly"),
    _f("decisions_dir", "Advanced", "Decisions directory", kind="path", storage="readonly"),
    _f("policy_file", "Advanced", "Policy file", "Edit policy rules in the Policy tab.", "path", storage="readonly"),
    _f("api_base_url", "Advanced", "API base URL", risk="sensitive"),
]

FIELDS: dict[str, Field] = {f.key: f for f in _FIELDS}
assert len(FIELDS) == len(_FIELDS), "duplicate registry key"

_overridden: set[str] = set()
_seen_version: str | None = None


# --------------------------------------------------------------------------- values
def coerce(field: Field, raw: Any) -> Any:
    """Validate and convert user input to the field's type. Raises ValueError with a message fit for the UI."""
    k = field.kind
    try:
        if k == "bool":
            if isinstance(raw, bool):
                return raw
            if str(raw).strip().lower() in {"true", "1", "yes", "on"}:
                return True
            if str(raw).strip().lower() in {"false", "0", "no", "off"}:
                return False
            raise ValueError("must be on or off")
        if k in ("int", "float"):
            val = int(str(raw).strip()) if k == "int" else float(str(raw).strip())
            if field.min is not None and val < field.min:
                raise ValueError(f"must be at least {field.min:g}")
            if field.max is not None and val > field.max:
                raise ValueError(f"must be at most {field.max:g}")
            return val
        if k == "enum":
            if str(raw) not in field.choices:
                raise ValueError(f"must be one of: {', '.join(field.choices)}")
            return str(raw)
        if k == "list":
            items = raw if isinstance(raw, (list, tuple)) else str(raw).replace("\n", ",").split(",")
            return [str(i).strip() for i in items if str(i).strip()]
        if k == "pair":
            parts = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
            if len(parts) != 2:
                raise ValueError("needs two numbers: input, output")
            a, b = float(parts[0]), float(parts[1])
            if a < 0 or b < 0:
                raise ValueError("must not be negative")
            return (a, b)
        if k == "schedule":
            if not is_valid_schedule(str(raw)):
                raise ValueError("use 30s, 15m, 6h or cron:M H D M W")
            return str(raw).strip().lower()
        if k == "time":
            text = str(raw).strip()
            if text:
                h, _, m = text.partition(":")
                if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60 and len(m) == 2):
                    raise ValueError("use HH:MM")
            return text
        if k in ("path", "secret"):
            raise ValueError("this value is not editable here")
        text = str(raw).strip()
        if any(c in text for c in "\n\r\x00"):
            raise ValueError("must be a single line")
        return text
    except ValueError as exc:
        raise ValueError(f"{field.label}: {exc}") from None


def _db_get(key: str) -> str | None:
    with session() as s:
        row = s.get(Setting, key)
        return row.value if row else None


def _db_put(key: str, value: str | None) -> None:
    with session() as s:
        row = s.get(Setting, key)
        if value is None:
            if row:
                s.delete(row)
        elif row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))


def config_version() -> str:
    return _db_get(VERSION_KEY) or "0"


def bump_version() -> str:
    new = str(int(config_version()) + 1)
    _db_put(VERSION_KEY, new)
    return new


def _jsonable(v: Any) -> Any:
    return list(v) if isinstance(v, tuple) else v


def env_is_set(field: Field) -> bool:
    return secrets.has(field.env)


def value(key: str) -> Any:
    """Current effective value. Secrets return only whether they are set (never the value)."""
    f = FIELDS[key]
    if f.storage == "secret" or f.kind == "secret":
        return env_is_set(f)
    if f.storage == "config" or f.storage == "readonly":
        return _jsonable(getattr(get_settings(), key, None))
    if f.storage == "env":
        raw = secrets.get(f.env)
        if raw is None:
            return None
        try:
            return coerce(f, raw)
        except ValueError:
            return raw
    if f.storage == "db":
        raw = _db_get(key)
        return None if raw is None else coerce(f, raw)
    return None


def source(key: str) -> str:
    """Where the current value comes from: ui | .env | default (managed/readonly keys report their owner)."""
    f = FIELDS[key]
    if f.storage in ("managed", "readonly"):
        return f.storage
    if f.storage == "config":
        if _db_get(CONFIG_PREFIX + key) is not None:
            return "ui"
        return ".env" if env_is_set(f) else "default"
    if f.storage == "db":
        return "ui" if _db_get(key) is not None else "default"
    return ".env" if env_is_set(f) else "default"


def secret_hint(key: str) -> dict[str, Any]:
    """What the UI may show about a secret: set or not, and the last 4 characters. Never the value."""
    f = FIELDS[key]
    raw = secrets.get(f.env) or ""
    return {"set": bool(raw), "last4": raw[-4:] if len(raw) >= 12 else ""}


# --------------------------------------------------------------------------- applying
def _apply_attr(key: str, val: Any) -> None:
    s = get_settings()
    if hasattr(s, key):
        setattr(s, key, tuple(val) if FIELDS[key].kind == "pair" else val)


def _default_from_env(key: str) -> Any:
    return getattr(Settings(), key)


def apply_overrides() -> list[str]:
    """Apply every ``config:*`` override to the live Settings object; restore fields whose override was removed."""
    global _seen_version, _overridden
    with session() as s:
        rows = {r.key[len(CONFIG_PREFIX):]: r.value for r in s.query(Setting).filter(Setting.key.like(CONFIG_PREFIX + "%"))}
    applied: set[str] = set()
    for key, raw in rows.items():
        f = FIELDS.get(key)
        if f is None or f.storage != "config":
            continue  # unknown or no longer editable: ignore a stale row
        try:
            _apply_attr(key, coerce(f, json.loads(raw)))
            applied.add(key)
        except ValueError:
            continue
    for key in _overridden - applied:
        _apply_attr(key, _default_from_env(key))
    _overridden = applied
    _seen_version = config_version()
    return sorted(applied)


def refresh_if_changed() -> bool:
    """Cheap per-tick check: re-apply overrides and reload .env when another process changed a setting."""
    if config_version() == _seen_version:
        return False
    secrets.reload()
    apply_overrides()
    return True


# --------------------------------------------------------------------------- changing
class StepUpRequired(PermissionError):
    """A critical setting needs the dashboard password again (the page checks it, then passes step_up_ok=True)."""


def needs_step_up(key: str) -> bool:
    return FIELDS[key].risk == "critical"


def _announce(text: str, key: str) -> None:
    """Tell the owner on every paired channel about a risky change (best effort; notify is built in a later wave)."""
    if FIELDS[key].risk == "normal":
        return
    try:
        from aecom import notify

        notify.route({"kind": "settings", "text": text, "urgency": "info"})
    except Exception:  # noqa: BLE001 - never block a save because a notification channel is down
        pass


def _log(by: str, key: str, old: Any, new: Any, src: str) -> None:
    f = FIELDS[key]
    detail = {"key": key, "section": f.section, "risk": f.risk, "source": src}
    if f.kind == "secret" or f.storage == "secret":
        detail["changed"] = True
    else:
        detail.update(old=old, new=new)
    audit(f"human:{by}" if by else "system", "setting_change", "ok", detail)


def set_value(key: str, raw: Any, by: str, *, step_up_ok: bool = False) -> Any:
    """Validate and store one setting; applies it live. Returns the stored (coerced) value; secrets return True."""
    if key not in FIELDS:
        raise KeyError(key)
    f = FIELDS[key]
    if f.storage in ("managed", "readonly"):
        raise ValueError(f"{f.label} is not edited here" + (f" (use {f.link})" if f.link else ""))
    if not by.strip():
        raise ValueError("a name is needed to record who changed this")
    if needs_step_up(key) and not step_up_ok:
        raise StepUpRequired(f"{f.label} needs your dashboard password")
    if f.storage == "secret":
        text = str(raw).strip()
        if not text or any(c in text for c in "\n\r\x00") or len(text) > 2000:
            raise ValueError(f"{f.label}: enter the value on one line")
        envfile.set_values({f.env: text})
        if f.env in os.environ:  # a process-level variable would otherwise keep winning over the new .env value
            os.environ[f.env] = text
        secrets.reload()
        _apply_attr(key, text)
        _log(by, key, None, None, "secret")
        bump_version()
        _announce(f"{f.label} was changed in the dashboard by {by}", key)
        return True
    val = coerce(f, raw)
    old = value(key)
    if f.storage == "config":
        _db_put(CONFIG_PREFIX + key, json.dumps(_jsonable(val)))
        _apply_attr(key, val)
        _overridden.add(key)
    elif f.storage == "db":
        _db_put(key, str(val))
    else:  # env
        text = ("true" if val else "false") if isinstance(val, bool) else str(val)
        envfile.set_values({f.env: text})
        if f.env in os.environ:
            os.environ[f.env] = text
        secrets.reload()
    _log(by, key, old, _jsonable(val), "ui")
    bump_version()
    _announce(f"{f.label} changed to {_jsonable(val)} by {by}", key)
    return val


def reset_value(key: str, by: str, *, step_up_ok: bool = False) -> None:
    """Remove the UI override so the .env value (or the built-in default) applies again."""
    f = FIELDS[key]
    if f.storage not in ("config", "db"):
        raise ValueError(f"{f.label} has no UI override to reset")
    if needs_step_up(key) and not step_up_ok:
        raise StepUpRequired(f"{f.label} needs your dashboard password")
    old = value(key)
    if f.storage == "config":
        _db_put(CONFIG_PREFIX + key, None)
        _apply_attr(key, _default_from_env(key))
        _overridden.discard(key)
    else:
        _db_put(key, None)
    _log(by, key, old, "reset", "ui")
    bump_version()


# --------------------------------------------------------------------------- history, export/import
def history(limit: int = 100, key: str | None = None) -> list[dict[str, Any]]:
    with session() as s:
        rows = s.query(AuditLog).filter(AuditLog.action == "setting_change").order_by(AuditLog.id.desc()).limit(limit * 3)
        out = []
        for r in rows:
            d = r.detail or {}
            if key and d.get("key") != key:
                continue
            out.append({"at": r.ts, "by": r.actor.removeprefix("human:"), **{k: d.get(k) for k in ("key", "old", "new", "changed", "risk")}})
            if len(out) >= limit:
                break
        return out


def export_settings() -> dict[str, Any]:
    """Non-secret, non-critical values that differ from the defaults, to copy to another machine (e.g. the cloud server)."""
    out: dict[str, Any] = {}
    for key, f in FIELDS.items():
        if f.storage in ("config", "db", "env") and f.kind != "secret" and f.risk != "critical" and source(key) != "default":
            out[key] = value(key)
    return out


def import_settings(data: dict[str, Any], by: str) -> dict[str, list[str]]:
    """Apply an export. Unknown keys, secrets and critical settings are skipped and reported; bad values too."""
    result: dict[str, list[str]] = {"applied": [], "skipped": [], "errors": []}
    for key, raw in data.items():
        f = FIELDS.get(key)
        if f is None or f.storage not in ("config", "db", "env") or f.kind == "secret" or f.risk == "critical":
            result["skipped"].append(str(key))
            continue
        try:
            set_value(key, raw, by)
            result["applied"].append(key)
        except ValueError as exc:
            result["errors"].append(str(exc))
    return result


def now_iso() -> str:
    return now().isoformat()
