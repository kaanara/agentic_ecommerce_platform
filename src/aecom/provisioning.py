"""E1/E4/E7: the provisioning loop - from an agent's need to an integrated vendor.

    need (any agent: request_account_need) → CEO decides → request_provisioning (T3, human)
      approved  → vendor 'provisioning' + checklist of the human steps
      rejected  → vendor 'rejected'; the reason is kept in the CEO's memory so it isn't requested again blindly
    credential appears in the env (check_credentials, every 15 min) → vendor 'active' → Foreman integrates it

Agents never sign up or handle credentials: this module only checks that the env var named by the vendor is set.
It never reads the value into a log, an audit row, an event or a prompt.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from aecom.audit import audit
from aecom.bus.events import publish
from aecom.company_settings import get_setting, set_setting
from aecom.db import Approval, Memory, Vendor, now, session
from aecom.secrets import secrets

MEMORY_NAMESPACE = "provisioning"
FOLLOWUP_MONITOR = "provisioning_followup"


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


# ----------------------------------------------------------------------------- credential check
def verify(vendor: dict[str, Any]) -> tuple[bool, str | None]:
    """Is the vendor's credential usable? Returns (ok, error); the error never contains the credential.

    E6 hook: today this only checks that the env var is set. Real per-vendor API verification goes here."""
    env = vendor.get("credential_env")
    if not env:
        return False, "no credential_env set"
    if not secrets.has(env):
        return False, f"{env} is not set"
    return True, None


def check_credentials() -> dict[str, Any]:
    """Activate vendors in 'provisioning' whose credential has arrived, then give the Foreman the integration task."""
    with session() as s:
        pending = [{"name": v.name, "category": v.category, "credential_env": v.credential_env}
                   for v in s.scalars(select(Vendor).where(Vendor.status == "provisioning",
                                                           Vendor.credential_env.is_not(None)))]
    activated: list[str] = []
    for snap in pending:
        try:
            ok, error = verify(snap)
        except Exception as exc:  # a broken check must not stop the others; the message is not the credential
            ok, error = False, f"verification failed: {type(exc).__name__}"
        with session() as s:
            v = s.scalars(select(Vendor).where(Vendor.name == snap["name"])).first()
            if v is None or v.status != "provisioning":
                continue
            if not ok:
                v.last_check_error = error
                continue
            v.status, v.verified_at, v.last_check_error = "active", now(), None
            publish("vendor.provisioned", {"vendor": snap["name"], "category": snap["category"]},
                    source="provisioning", session=s)
        audit("system", "provisioning", "activated", {"vendor": snap["name"], "credential_env": snap["credential_env"]})
        activated.append(snap["name"])
        _foreman_followup(snap)
    return {"checked": len(pending), "activated": activated}


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

