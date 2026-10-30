"""E1/E4/E5/E6/E7: the provisioning loop - from an agent's need to an integrated vendor.

    need (any agent: request_account_need) → CEO decides → request_provisioning (T3, human)
      approved  → vendor 'provisioning' + checklist + "action needed" email + P3 incident provisioning_action
      rejected  → vendor 'rejected'; the reason is kept in the CEO's memory so it isn't requested again blindly
    check_credentials (every 15 min):
      credential set → one read-only API call by the vendor's verifier (VERIFIERS)
        ok   → mode 'auto': 'active' (+ warnings) → email → Foreman integrates it; the P3 incident resolves
               mode 'human_confirm': stays 'provisioning' (verified_at set), ONE email asking the human to run
               `aecom provisioning confirm VENDOR --by NAME`, which activates and hands off the same way
        fail → stays 'provisioning', masked error in last_check_error, the human gets an email
      still waiting 3+ days after approval → reminder email every 3 days (`aecom provisioning snooze` defers it)

The mode (U3) is proposed by the CEO inside request_provisioning and approved with it by the human; unset, it is
'auto' for vendors with a built-in verifier and 'human_confirm' for unknown ones. No agent can activate a vendor.

Agents never sign up or handle credentials. The value is read from SecretsProvider only to make the verification
call; it never goes into a log, an audit row, an event, an email, the DB or a prompt (only the env var name does).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import func, select

from aecom.audit import audit
from aecom.bus.events import publish
from aecom.company_settings import get_setting, set_setting
from aecom.config import get_settings
from aecom.db import Approval, Memory, Vendor, now, session
from aecom.incidents import raise_incident, resolve_absent
from aecom.integrations.agentmail import AgentMail
from aecom.observability import MASK, mask, record_error
from aecom.secrets import secrets

log = logging.getLogger(__name__)
db_now = now  # `now` is also a parameter name below

ACTIVATION_MODES = ("auto", "human_confirm")
CONFIRM_STEP = "Confirm the vendor works (human)"
MEMORY_NAMESPACE = "provisioning"
FOLLOWUP_MONITOR = "provisioning_followup"
ACTION_MONITOR = "provisioning_action"  # P3 while a human still has to provision a vendor
REMIND_AFTER = timedelta(days=3)
VERIFY_TIMEOUT = 10
ANTHROPIC_VERSION = "2023-06-01"
SCOPE_WORDS = re.compile(r"(?i)\b(scopes?|permissions?|roles?|access|read[- ]only|read|write|admin)\b")


def rejection_reason(vendor: str) -> str | None:
    """The latest stored human rejection for this vendor (CEO memory), if any."""
    with session() as s:
        m = s.scalars(select(Memory).where(Memory.agent_id == "ceo", Memory.namespace == MEMORY_NAMESPACE,
                                           Memory.content.startswith(f"Rejected {vendor}: "))
                      .order_by(Memory.created_at.desc())).first()
        return m.content if m else None


# ----------------------------------------------------------------------------- approval.resolved
def on_approval_resolved(evt: dict[str, Any]) -> None:
    """Executed → set the vendor's checklist; rejected → vendor 'rejected' + reason in the CEO's memory. Idempotent."""
    if evt.get("tool") != "request_provisioning" or evt.get("status") not in ("executed", "rejected"):
        return
    with session() as s:
        a = s.get(Approval, evt["approval_id"])
        if a is None:
            return
        args, note = dict(a.args or {}), evt.get("note") or a.decision_note or ""
    vendor = args.get("vendor")
    if not vendor:
        return
    if evt["status"] == "executed":
        _set_checklist(vendor, args.get("steps") or [], evt["approval_id"])
        _action_needed(vendor, args, evt["approval_id"])
    else:
        _reject(vendor, args, note, evt["approval_id"])


def _set_checklist(vendor: str, steps: list[str], approval_id: str) -> None:
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == vendor)).first()
        if v is None or v.status != "provisioning" or v.checklist:
            return
        v.checklist = [{"step": str(step), "done": False} for step in steps]
    audit("system", "provisioning", "checklist_set", {"vendor": vendor, "steps": len(steps), "approval_id": approval_id})


def _reject(vendor: str, args: dict[str, Any], note: str, approval_id: str) -> None:
    reason = note.strip() or "no reason given"
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == vendor)).first()
        if v is not None and v.status in ("provisioning", "active"):  # a rejected duplicate must not undo a live vendor
            audit("system", "provisioning", "rejection_ignored", {"vendor": vendor, "status": v.status,
                                                                   "approval_id": approval_id})
            return
        if v is None:
            v = Vendor(name=vendor, category=args.get("category", "api"),
                       monthly_cost_eur=float(args.get("monthly_cost_eur") or 0.0))
            s.add(v)
        v.status = "rejected"
        v.notes = f"Rejected by a human: {reason}" + (f"\n\n{v.notes}" if v.notes else "")
        content = f"Rejected {vendor}: {reason}"
        if s.scalars(select(Memory).where(Memory.agent_id == "ceo", Memory.namespace == MEMORY_NAMESPACE,
                                          Memory.content == content)).first() is None:
            s.add(Memory(agent_id="ceo", namespace=MEMORY_NAMESPACE, content=content))
    audit("system", "provisioning", "rejected", {"vendor": vendor, "note": reason, "approval_id": approval_id})


# ----------------------------------------------------------------------------- telling the human (E5)
def _notify(subject: str, body: str) -> bool:
    """Email ALERT_EMAIL (outbox unless AGENTMAIL_LIVE). No-op without it; never raises. Text is secret-masked."""
    to = secrets.get("ALERT_EMAIL")
    if not to:
        return False
    try:
        AgentMail().send(to, mask(subject), mask(body))
        return True
    except Exception as exc:
        log.warning("provisioning email failed: %s", mask(str(exc)))
        record_error("provisioning", exc, context={"channel": "email", "subject": mask(subject)})
        return False


def _scopes(steps: list[str]) -> list[str]:
    """The steps that name scopes, permissions, roles or access levels (so the human sees what to grant)."""
    return [s for s in steps if SCOPE_WORDS.search(s)]


def _instructions(vendor: str, args: dict[str, Any], checklist: list[dict[str, Any]] | None = None) -> str:
    steps = [str(x) for x in (args.get("steps") or [])]
    if checklist:
        lines = [f"  {n}. [{'x' if c.get('done') else ' '}] {c.get('step')}" for n, c in enumerate(checklist, 1)]
    else:
        lines = [f"  {n}. {s}" for n, s in enumerate(steps, 1)] or ["  (no steps given)"]
    scopes = _scopes(steps) or ["none named in the steps - grant the least access that works (read-only if possible)"]
    cost = float(args.get("monthly_cost_eur") or 0.0)
    return "\n".join([
        f"Vendor: {vendor} ({args.get('category') or '-'})",
        f"Purpose: {args.get('purpose') or '-'}",
        f"Monthly cost: EUR {cost:.2f}",
        "",
        "Steps:",
        *lines,
        "",
        f"Credential: put it in .env as {args.get('credential_env') or '(no env var named)'} - never paste it in chat "
        "or email.",
        "Scopes / permissions the steps ask for:",
        *[f"  - {s}" for s in scopes],
        "",
        "The app checks the credential every 15 minutes with one read-only call. "
        + ("It then waits for your `aecom provisioning confirm` before activating the vendor."
           if args.get("activation") == "human_confirm" else "The vendor is activated when it works."),
        f'Tick a step:  uv run aecom provisioning done "{vendor}" STEP_NO --by NAME',
        f'Snooze:       uv run aecom provisioning snooze "{vendor}" --days N',
    ])


def _action_needed(vendor: str, args: dict[str, Any], approval_id: str) -> None:
    """P3 incident (dashboard/digest) + 'action needed' email, once per provisioning round."""
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == vendor)).first()
        if v is None or v.status != "provisioning":
            return
        checklist = list(v.checklist or [])
    body = _instructions(vendor, args, checklist)
    _, is_new = raise_incident(ACTION_MONITOR, "P3", subject=vendor,
                               detail=f"Action needed: a human provisions {vendor} (approval {approval_id}).\n\n{body}")
    if is_new:
        _notify(f"[action needed] Provision {vendor}", body)
        audit("system", "provisioning", "action_needed", {"vendor": vendor, "approval_id": approval_id,
                                                          "credential_env": args.get("credential_env")})


def _approved_at(vendor: str) -> datetime | None:
    """When the latest request_provisioning for this vendor was approved."""
    with session() as s:
        rows = s.scalars(select(Approval).where(Approval.tool_name == "request_provisioning",
                                                Approval.status == "executed")
                         .order_by(Approval.decided_at.desc()))
        return next((a.decided_at or a.created_at for a in rows if (a.args or {}).get("vendor") == vendor), None)


def _remind(snap: dict[str, Any], t: datetime) -> bool:
    """Reminder email for a vendor still provisioning REMIND_AFTER after approval, at most every REMIND_AFTER."""
    since = _approved_at(snap["name"])
    last = snap.get("last_reminded_at")
    if since is None or t - since < REMIND_AFTER or (last is not None and t - last < REMIND_AFTER):
        return False
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == snap["name"])).first()
        if v is None or v.status != "provisioning":
            return False
        v.last_reminded_at = t
        args = {"category": v.category, "purpose": v.notes.split("\n\n")[0] if v.notes else "",
                "monthly_cost_eur": v.monthly_cost_eur, "credential_env": v.credential_env,
                "steps": [c.get("step") for c in v.checklist or []]}
        checklist, error = list(v.checklist or []), v.last_check_error
    days = (t - since).days
    status = f"Last check: {error}" if error else "Last check: no credential yet"
    _notify(f"[reminder] {snap['name']} is still waiting for provisioning ({days} days)",
            f"{snap['name']} was approved {days} days ago and is still not active.\n{status}\n\n"
            + _instructions(snap["name"], args, checklist))
    audit("system", "provisioning", "reminded", {"vendor": snap["name"], "days": days})
    return True


# ----------------------------------------------------------------------------- checklist (CLI)
def _find(s: Any, vendor: str) -> Vendor:
    v = s.scalars(select(Vendor).where(func.lower(Vendor.name) == vendor.strip().lower())).first()
    if v is None:
        raise ValueError(f"unknown vendor {vendor!r}")
    return v


def list_provisioning() -> list[dict[str, Any]]:
    """Vendors waiting for a human, with their numbered checklist (never the credential, only its env var name)."""
    with session() as s:
        return [{"vendor": v.name, "category": v.category, "credential_env": v.credential_env,
                 "activation": effective_activation(v), "activation_reason": v.activation_reason,
                 "waiting_for_your_confirmation": bool(v.verified_at) and effective_activation(v) == "human_confirm",
                 "monthly_cost_eur": v.monthly_cost_eur, "last_check_error": v.last_check_error,
                 "last_reminded_at": v.last_reminded_at,
                 "checklist": [{"no": n, "step": c.get("step"), "done": bool(c.get("done"))}
                               for n, c in enumerate(v.checklist or [], 1)]}
                for v in s.scalars(select(Vendor).where(Vendor.status == "provisioning").order_by(Vendor.name))]


def tick_step(vendor: str, step_no: int, by: str) -> dict[str, Any]:
    """Mark checklist step `step_no` (1-based) done. Audited as the human."""
    if not by.strip():
        raise ValueError("--by NAME is required")
    with session() as s:
        v = _find(s, vendor)
        steps = [dict(c) for c in v.checklist or []]
        if not 1 <= step_no <= len(steps):
            raise ValueError(f"{v.name} has steps 1..{len(steps)}, not {step_no}")
        steps[step_no - 1]["done"] = True
        v.checklist, name = steps, v.name  # reassign: JSON columns don't track in-place changes
        out = {"vendor": name, "step": step_no, "done": True, "remaining": sum(1 for c in steps if not c["done"])}
    audit(f"human:{by}", "provisioning", "step_done", {"vendor": name, "step": step_no})
    return out


def snooze(vendor: str, days: int, by: str = "cli-user", now: datetime | None = None) -> dict[str, Any]:
    """Defer reminders: last_reminded_at moves `days` into the future, so the next one comes REMIND_AFTER later."""
    if days < 1:
        raise ValueError("--days must be at least 1")
    until = (now or db_now()) + timedelta(days=days)
    with session() as s:
        v = _find(s, vendor)
        v.last_reminded_at, name = until, v.name
    audit(f"human:{by}", "provisioning", "snoozed", {"vendor": name, "days": days})
    return {"vendor": name, "last_reminded_at": until, "next_reminder_after": until + REMIND_AFTER}


# ----------------------------------------------------------------------------- credential check (E6)
Verdict = tuple[bool, str | None, list[str]]  # (ok, error, warnings); never contains the credential


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _http_get(url: str, headers: dict[str, str]) -> httpx.Response:
    return httpx.get(url, headers=headers, timeout=VERIFY_TIMEOUT)


def _http_error(r: httpx.Response, what: str) -> str:
    return f"{what} answered HTTP {r.status_code} {r.reason_phrase}".rstrip()


def verify_lemonsqueezy(vendor: dict[str, Any], credential: str) -> Verdict:
    r = _http_get("https://api.lemonsqueezy.com/v1/users/me",
                  {"Authorization": f"Bearer {credential}", "Accept": "application/vnd.api+json"})
    return (True, None, []) if r.status_code == 200 else (False, _http_error(r, "Lemon Squeezy /v1/users/me"), [])


def verify_agentmail(vendor: dict[str, Any], credential: str) -> Verdict:
    settings, headers = get_settings(), {"Authorization": f"Bearer {credential}"}
    base, inbox = settings.agentmail_base_url.rstrip("/"), settings.agentmail_org_inbox
    r = _http_get(f"{base}/inboxes/{quote(inbox, safe='@')}", headers)
    if r.status_code != 200:
        return False, _http_error(r, f"AgentMail inbox {inbox}"), []
    warnings = []
    try:  # an inbox-scoped key gets 403 here; success means the key can manage the whole organization
        if _http_get(f"{base}/api-keys", headers).status_code == 200:
            warnings.append("key has organization-wide access; create an inbox-scoped key")
    except httpx.HTTPError:
        pass  # the scope probe is advisory; the inbox call above already proved the key works
    return True, None, warnings


def verify_anthropic(vendor: dict[str, Any], credential: str) -> Verdict:
    r = _http_get("https://api.anthropic.com/v1/models",
                  {"x-api-key": credential, "anthropic-version": ANTHROPIC_VERSION})
    return (True, None, []) if r.status_code == 200 else (False, _http_error(r, "Anthropic /v1/models"), [])


# keyed by vendor name or category, normalised (lower-case, letters and digits only); each does ONE read-only call
VERIFIERS: dict[str, Callable[[dict[str, Any], str], Verdict]] = {
    "lemonsqueezy": verify_lemonsqueezy,
    "agentmail": verify_agentmail,
    "anthropic": verify_anthropic,
}


def has_verifier(vendor: dict[str, Any]) -> bool:
    return bool(VERIFIERS.get(_key(vendor.get("name", ""))) or VERIFIERS.get(_key(vendor.get("category", ""))))


def effective_activation(vendor: Vendor | dict[str, Any]) -> str:
    """'auto' | 'human_confirm'. An explicit value wins; otherwise auto if a built-in verifier exists, else human."""
    get = vendor.get if isinstance(vendor, dict) else lambda k, d=None: getattr(vendor, k, d)
    explicit = get("activation")
    if explicit in ACTIVATION_MODES:
        return explicit
    return "auto" if has_verifier({"name": get("name") or "", "category": get("category") or ""}) else "human_confirm"


def _scrub(text: str, credential: str | None) -> str:
    if credential:
        text = text.replace(credential, MASK)
    return mask(text)


def verify(vendor: dict[str, Any]) -> Verdict:
    """Is the vendor's credential usable? One read-only call by the vendor's verifier; never raises.

    Unknown vendors are checked for presence only (with a warning). Errors and warnings never contain the credential."""
    env = vendor.get("credential_env")
    if not env:
        return False, "no credential_env set", []
    if not secrets.has(env):
        return False, f"{env} is not set", []
    fn = VERIFIERS.get(_key(vendor.get("name", ""))) or VERIFIERS.get(_key(vendor.get("category", "")))
    if fn is None:
        return True, None, [f"{env} is set but was not verified (no verifier for {vendor.get('name')}); "
                            "only its presence was checked"]
    credential = secrets.get(env) or ""
    try:
        ok, error, warnings = fn(vendor, credential)
    except httpx.HTTPError as exc:  # timeouts, DNS, refused: a failed check, never an exception
        ok, error, warnings = False, f"network error: {type(exc).__name__}: {exc}", []
    except Exception as exc:
        ok, error, warnings = False, f"verification failed: {type(exc).__name__}", []
    return ok, _scrub(error, credential) if error else None, [_scrub(w, credential) for w in warnings]


def check_credentials(now: datetime | None = None) -> dict[str, Any]:
    """Verify vendors in 'provisioning' whose credential has arrived; activate them and hand them to the Foreman.

    Vendors still waiting get a reminder email every REMIND_AFTER. Never raises for one vendor's failure."""
    t = now or db_now()
    with session() as s:
        pending = [{"name": v.name, "category": v.category, "credential_env": v.credential_env,
                    "last_check_error": v.last_check_error, "last_reminded_at": v.last_reminded_at,
                    "activation": v.activation}
                   for v in s.scalars(select(Vendor).where(Vendor.status == "provisioning"))]
    activated: list[str] = []
    reminded: list[str] = []
    for snap in pending:
        try:
            if snap["credential_env"] and _check_one(snap):
                activated.append(snap["name"])
                continue
        except Exception as exc:
            record_error("provisioning", exc, context={"vendor": snap["name"], "step": "check"})
        try:
            if _remind(snap, t):
                reminded.append(snap["name"])
        except Exception as exc:
            record_error("provisioning", exc, context={"vendor": snap["name"], "step": "reminder"})
    if activated:
        resolve_absent(ACTION_MONITOR, _provisioning_names(), now=t)
    out: dict[str, Any] = {"checked": sum(1 for p in pending if p["credential_env"]), "activated": activated}
    if reminded:
        out["reminded"] = reminded
    return out


def _check_one(snap: dict[str, Any]) -> bool:
    """Verify one vendor; True if it became active. The credential is only named, never stored or sent."""
    name, env = snap["name"], snap["credential_env"]
    present = secrets.has(env)
    try:
        ok, error, warnings = verify(snap)
    except Exception as exc:  # a broken check must not stop the others; the message is not the credential
        ok, error, warnings = False, f"verification failed: {type(exc).__name__}", []
    mode = effective_activation(snap)
    first_wait = False
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == name)).first()
        if v is None or v.status != "provisioning":
            return False
        if not ok:
            changed = v.last_check_error != error
            v.last_check_error = error
            v.verified_at = None  # no longer a verified credential, so no longer "waiting for confirmation"
        elif mode == "human_confirm":
            first_wait = v.verified_at is None
            v.verified_at = v.verified_at or db_now()
            v.last_check_error = ("warning: " + "; ".join(warnings)) if warnings else None
            if first_wait and not any(c.get("step") == CONFIRM_STEP for c in v.checklist or []):
                v.checklist = [*(v.checklist or []), {"step": CONFIRM_STEP, "done": False}]
        else:
            _activate(s, v, warnings)
    if not ok:
        if present and changed:  # a credential is there but does not work: tell the human once per distinct error
            audit("system", "provisioning", "check_failed", {"vendor": name, "credential_env": env, "error": error})
            _notify(f"[provisioning] {name}: the credential in {env} does not work",
                    f"The credential check for {name} failed:\n\n  {error}\n\n"
                    f"{name} stays in 'provisioning'. Fix the credential in {env} (check the steps and the "
                    "scopes/permissions it was created with); the next check runs within 15 minutes.\n"
                    f"Checklist: uv run aecom provisioning list")
        return False
    if mode == "human_confirm":
        if first_wait:
            _await_confirmation(snap, warnings)
        return False
    _announce_active(snap, warnings, by=None)
    return True


def _activate(s: Any, v: Vendor, warnings: list[str]) -> None:
    v.status, v.verified_at = "active", db_now()
    v.last_check_error = ("warning: " + "; ".join(warnings)) if warnings else None
    publish("vendor.provisioned", {"vendor": v.name, "category": v.category}, source="provisioning", session=s)


def _announce_active(snap: dict[str, Any], warnings: list[str], by: str | None) -> None:
    name, env = snap["name"], snap["credential_env"]
    audit(f"human:{by}" if by else "system", "provisioning", "activated",
          {"vendor": name, "credential_env": env, "warnings": warnings, "confirmed_by": by})
    body = f"{name} passed its credential check ({env}) and is now active. The Foreman integrates it next."
    if warnings:
        body += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings)
    _notify(f"[provisioning] {name} is active" + (" (with warnings)" if warnings else ""), body)
    _foreman_followup(snap)


def _await_confirmation(snap: dict[str, Any], warnings: list[str]) -> None:
    """Credential verified but the vendor needs a human tick: update the P3 incident and email once."""
    name, env = snap["name"], snap["credential_env"]
    cmd = f'uv run aecom provisioning confirm "{name}" --by NAME'
    msg = f"The credential for {name} ({env}) was found and verified, but {name} needs your confirmation. Run: {cmd}"
    if warnings:
        msg += "\n\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings)
    raise_incident(ACTION_MONITOR, "P3", subject=name, detail=f"Waiting for your confirmation: {msg}")
    audit("system", "provisioning", "awaiting_confirmation", {"vendor": name, "credential_env": env,
                                                              "warnings": warnings})
    _notify(f"[action needed] Confirm {name}", msg)


def confirm(vendor: str, by: str, note: str = "") -> dict[str, Any]:
    """A human activates a provisioning vendor whose credential works. Then the usual hand-off to the Foreman."""
    if not by.strip():
        raise ValueError("--by NAME is required")
    with session() as s:
        v = _find(s, vendor)
        if v.status != "provisioning":
            raise ValueError(f"{v.name} is {v.status}, not waiting for provisioning")
        snap = {"name": v.name, "category": v.category, "credential_env": v.credential_env}
    if not snap["credential_env"] or not secrets.has(snap["credential_env"]):
        raise ValueError(f"{snap['name']} needs {snap['credential_env'] or 'a credential'} to be set before this step. "
                         "Add it in Settings, then come back here.")
    ok, error, warnings = verify(snap)
    if not ok:
        raise ValueError(f"{snap['name']}'s credential did not pass the check: {error}. Update it in Settings, then try again.")
    with session() as s:
        v = _find(s, snap["name"])
        if v.status != "provisioning":
            raise ValueError(f"{v.name} is {v.status}, not waiting for provisioning")
        steps = [dict(c) for c in v.checklist or []]
        for c in steps:
            if c.get("step") == CONFIRM_STEP:
                c["done"] = True
        v.checklist = steps
        _activate(s, v, warnings)
    audit(f"human:{by}", "provisioning", "confirmed", {"vendor": snap["name"], "note": note})
    _announce_active(snap, warnings, by=by)
    resolve_absent(ACTION_MONITOR, _provisioning_names())
    return {"vendor": snap["name"], "status": "active", "confirmed_by": by, "warnings": warnings}


def _provisioning_names() -> list[str]:
    with session() as s:
        return list(s.scalars(select(Vendor.name).where(Vendor.status == "provisioning")))


def _foreman_followup(snap: dict[str, Any]) -> None:
    """Ask the Foreman to integrate the vendor. If that can't happen, a P3 incident keeps it visible to a human."""
    from aecom.graph.agents import run_agent
    from aecom.handlers import run_sync
    from aecom.incidents import raise_incident

    task = (f"{snap['name']} is provisioned; integrate it: propose its MCP server or adapter with propose_tool, sandbox "
            f"it, and plan who needs it granted (check list_registry first so nothing is duplicated). It is a "
            f"{snap['category']} vendor; its credential is available to tools as the env var {snap['credential_env']} "
            "(you never see the value).")
    try:
        result = run_sync(run_agent("foreman", task))
        problem = None if result.status == "ok" else f"foreman {result.status}: {result.output[:300]}"
    except Exception as exc:
        problem = f"foreman run failed: {type(exc).__name__}"
    if problem:
        raise_incident(FOLLOWUP_MONITOR, "P3", subject=snap["name"],
                       detail=f"{snap['name']} is active but the integration task did not run ({problem}). "
                              "Ask the Foreman to integrate it.")


# ----------------------------------------------------------------------------- account needs → CEO
def _need_key(vendor: str) -> str:
    return f"account_need:{vendor.strip().lower()}:{now():%Y-%m-%d}"


def hand_need_to_ceo(evt: dict[str, Any]) -> None:
    """One CEO run per vendor per day decides on raised account needs. A failed run raises HandlerFailed (bus retries)."""
    from aecom.graph.agents import run_agent
    from aecom.handlers import ensure_ok, run_sync
    from aecom.tools.catalog import fence

    vendor, key = evt["vendor"], _need_key(evt["vendor"])
    if get_setting(key):
        audit("system", "account_need", "deduplicated", {"vendor": vendor, "raised_by": evt.get("raised_by"),
                                                          "ceo_run": get_setting(key)})
        return
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == vendor)).first()
        state = f"Current vendor status: {v.status}." if v else "This vendor is not registered yet."
    rejected = rejection_reason(vendor)
    history = f" A human rejected it before ({rejected}); only re-request if something has changed." if rejected else ""
    need = f"why: {evt.get('why', '')}\nevidence: {evt.get('evidence', '') or '-'}"
    task = (f"{evt.get('raised_by', 'An agent')} raised an account need for vendor '{vendor}'.\n\n"
            f"{fence('agent:' + str(evt.get('raised_by', '')), need)}\n\n{state}{history}\n"
            "Decide following your provisioning procedure: prefer vendors we already have (list_vendors). If it is worth "
            "it, record_decision and call request_provisioning with concrete numbered human steps and the env var name "
            "for the credential. Otherwise say why not.")
    result = ensure_ok(run_sync(run_agent("ceo", task, run_id=evt.get("run_id"))))
    if result.status == "ok":
        set_setting(key, now().isoformat())
    audit("ceo", "account_need", "decided" if result.status == "ok" else result.status,
          {"vendor": vendor, "raised_by": evt.get("raised_by")})


def raise_need(vendor: str, why: str, evidence: str, raised_by: str) -> None:
    if not vendor.strip() or not why.strip():
        raise ValueError("vendor and why are required")
    detail = {"vendor": vendor.strip(), "why": why, "evidence": evidence, "raised_by": raised_by}
    audit(raised_by, "account_need", "raised", detail)
    publish("account.need_raised", detail, source=raised_by)

