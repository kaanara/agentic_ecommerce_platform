"""Phase 5 / G4: the trust report and evidence-gated promotion of autonomy stages.

``stats`` summarises how humans treated a tool's escalations; ``eligibility`` says whether the next stage may be
earned; ``promote`` re-checks that on the server (no override) before calling ``autonomy.set_stage`` with the evidence.
Only the per-call limits ever move: budgets, tiers, recipient rule, kill switch, pause and safe mode stay as they are.
Demotion is always back to stage 1. There is deliberately no agent tool for any of this.

R19: the eligibility check and the stage write in ``promote`` are not an unguarded two-step - the write is a
compare-and-set against the exact stage eligibility saw (``autonomy.set_stage(..., expected_old_stage=...)``), so a
promotion or demotion that lands in between is detected instead of silently overwritten.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any

from sqlalchemy import select

from aecom import autonomy, incidents
from aecom.company_settings import kill_switch_on, safe_mode_on
from aecom.db import Approval, AuditLog, Order, session
from aecom.db import now as db_now
from aecom.secrets import secrets

REAL_STATUSES = ("paid", "delivered", "partially_refunded")
APPROVED = ("approved", "executed", "failed")  # a human said yes; executed/failed is what happened afterwards
THRESHOLDS = {"AUTONOMY_MIN_REAL_SALES": 10, "AUTONOMY_MIN_DECISIONS": 20,
              "AUTONOMY_MIN_UNCHANGED_RATE": 0.95, "AUTONOMY_COOLDOWN_DAYS": 14}


def _threshold(name: str) -> float:
    default = THRESHOLDS[name]
    try:
        return type(default)(secrets.get(name) or default)
    except (TypeError, ValueError):
        return default


def thresholds() -> dict[str, float]:
    return {k: _threshold(k) for k in THRESHOLDS}


def real_sales() -> int:
    """Orders that came from the real store (external_id set) and were actually paid."""
    with session() as s:
        return len(s.scalars(select(Order.id).where(Order.external_id.is_not(None),
                                                    Order.status.in_(REAL_STATUSES))).all())


def _ref(a: Approval) -> datetime:
    return a.decided_at or a.created_at


def _autonomous_rows(tool: str, since: datetime | None = None) -> list[AuditLog]:
    with session() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.action == tool).order_by(AuditLog.id)).all()
        return [r for r in rows if (r.detail or {}).get("autonomous") is True and (since is None or r.ts >= since)]


def stats(tool: str, since: datetime | None = None) -> dict[str, Any]:
    """Approval statistics for one tool. ``since`` filters by decision time (creation time while still pending)."""
    with session() as s:
        rows = [a for a in s.scalars(select(Approval).where(Approval.tool_name == tool))
                if since is None or _ref(a) >= since]
    approved = [a for a in rows if a.status in APPROVED]
    rejected = [a for a in rows if a.status == "rejected"]
    decided = len(approved) + len(rejected)
    edited = sum(bool(a.args_edited) for a in approved)
    minutes = [(a.decided_at - a.created_at).total_seconds() / 60 for a in approved + rejected if a.decided_at]
    last = max((a.decided_at for a in approved + rejected if a.decided_at), default=None)
    return {
        "escalations": len(rows), "decided": decided, "approved": len(approved), "rejected": len(rejected),
        "expired": sum(a.status == "expired" for a in rows), "pending": sum(a.status == "pending" for a in rows),
        "edited": edited,
        "unchanged_rate": round((len(approved) - edited) / decided, 4) if decided else None,
        "unchanged_exact": (len(approved) - edited) / decided if decided else None,  # eligibility compares THIS, unrounded
        "reject_rate": round(len(rejected) / decided, 4) if decided else None,
        "median_decision_minutes": round(median(minutes), 1) if minutes else None,
        "autonomous_actions": len(_autonomous_rows(tool, since)),
        "last_decision_at": last.isoformat() if last else None,
    }


def _stage_changes(tool: str) -> list[AuditLog]:
    with session() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.action == "autonomy_stage").order_by(AuditLog.id)).all()
        return [r for r in rows if (r.detail or {}).get("tool") == tool]


def _p1_open() -> bool:
    return any(i["severity"] == "P1" and i["status"] in incidents.ACTIVE for i in incidents.list_incidents())


def eligibility(tool: str, now: datetime | None = None) -> dict[str, Any]:
    """Whether ``tool`` may move up one stage, with every missing criterion spelled out."""
    now = now or db_now()
    th = thresholds()
    stage = autonomy.current_stage(tool)
    promotable = autonomy.is_promotable(tool)
    higher = [n for n in autonomy.defined_stages(tool) if n > stage]
    nxt = higher[0] if higher else None
    changes = _stage_changes(tool)
    since = changes[-1].ts if changes else None
    st = stats(tool, since)
    sales = real_sales()
    demoted = [c.ts for c in changes if c.decision == "demoted"]
    cooldown_until = max(demoted) + timedelta(days=th["AUTONOMY_COOLDOWN_DAYS"]) if demoted else None
    evidence = {"real_sales": sales, "decisions": st["decided"], "approved": st["approved"], "rejected": st["rejected"],
                "expired": st["expired"], "edited": st["edited"], "unchanged_rate": st["unchanged_rate"],
                "window_since": since.isoformat() if since else None, "thresholds": th}
    missing: list[str] = []
    if not promotable:
        missing.append(f"{tool} is not a promotable action")
    elif nxt is None:
        missing.append(f"already at the highest defined stage ({stage})")
    if sales < th["AUTONOMY_MIN_REAL_SALES"]:
        missing.append(f"real sales {sales} < {th['AUTONOMY_MIN_REAL_SALES']:g}")
    if st["decided"] < th["AUTONOMY_MIN_DECISIONS"]:
        missing.append(f"decisions since the last stage change {st['decided']} < {th['AUTONOMY_MIN_DECISIONS']:g}")
    rate = st["unchanged_exact"]  # unrounded: 94.996% must not pass a 95% bar
    if rate is None or rate < th["AUTONOMY_MIN_UNCHANGED_RATE"]:
        shown = "n/a" if rate is None else f"{rate:.1%}"
        missing.append(f"approve-unchanged rate {shown} < {th['AUTONOMY_MIN_UNCHANGED_RATE']:.0%}")
    if st["rejected"]:
        missing.append(f"{st['rejected']} rejection(s) since the last stage change")
    if st["expired"]:
        missing.append(f"{st['expired']} expired approval(s) since the last stage change")
    if cooldown_until is not None and now < cooldown_until:
        missing.append(f"demoted recently; cooldown until {cooldown_until.date().isoformat()}")
    if _p1_open():
        missing.append("an open P1 incident")
    if safe_mode_on():
        missing.append("safe mode is on")
    if kill_switch_on():
        missing.append("kill switch is on")
    return {"tool": tool, "eligible": not missing, "stage": stage, "next_stage": nxt, "missing": missing,
            "evidence": evidence}


def promote(tool: str, by: str, reason: str = "", now: datetime | None = None) -> int:
    """Raise ``tool`` by exactly one stage if (re-checked here) it is eligible. No override exists.

    R19: the eligibility check above and the stage write below are not two independent steps - the write is a
    guarded compare-and-set (``autonomy.set_stage(..., expected_old_stage=e["stage"])``): if the stage on disk no
    longer matches what eligibility just saw (another promotion, or an automatic demotion, raced in between), the
    write is refused instead of silently applying a decision based on stale evidence. Call again to re-check."""
    if not (by or "").strip():
        raise ValueError("promotion needs the name of the human (by)")
    now = now or db_now()
    e = eligibility(tool, now)
    if not e["eligible"]:
        raise ValueError(f"{tool} is not eligible for promotion: " + "; ".join(e["missing"]))
    try:
        return autonomy.set_stage(tool, e["next_stage"], by, reason,
                                  evidence={**e["evidence"], "checked_at": now.isoformat()},
                                  expected_old_stage=e["stage"])
    except autonomy.StageConflict as exc:
        raise ValueError(str(exc)) from exc


def demote(tool: str, by: str, reason: str = "") -> int:
    """Human demotion to stage 1."""
    if not (by or "").strip():
        raise ValueError("demotion needs the name of the human (by)")
    if not autonomy.is_promotable(tool):
        raise ValueError(f"{tool} is not a promotable action")
    return autonomy.demote(tool, reason or "demoted by a human", by)


def promotable_tools() -> list[str]:
    from aecom.policy.gate import load_policy

    return [t for t in load_policy()["rules"] if autonomy.is_promotable(t)]


def report() -> list[dict[str, Any]]:
    """One row per promotable tool: eligibility plus stats (all-time)."""
    return [{**eligibility(t), "stages": autonomy.defined_stages(t), "stats": stats(t)} for t in promotable_tools()]


def _safe_args(args: Any) -> str:
    """Numbers and flags are shown (they are what a reviewer needs: amounts, percentages); every other value is
    reduced to its key so customer emails, names or free text never end up in the autonomy log."""
    if not isinstance(args, dict):
        return ""
    shown = {k: v for k, v in args.items() if isinstance(v, (int, float, bool)) and not isinstance(v, str)}
    hidden = sorted(k for k in args if k not in shown)
    return f"{shown}" + (f" (+ {', '.join(hidden)})" if hidden else "")


def log(days: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
    """Stage changes and autonomous actions of the last ``days`` days, oldest first."""
    since = (now or db_now()) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    with session() as s:
        for r in s.scalars(select(AuditLog).where(AuditLog.ts >= since).order_by(AuditLog.id)):
            d = r.detail or {}
            if r.action == "autonomy_stage":
                out.append({"ts": r.ts.isoformat(), "kind": r.decision, "tool": d.get("tool"), "actor": r.actor,
                            "detail": f"stage {d.get('from')} -> {d.get('to')}: {d.get('reason') or ''}".strip()})
            elif d.get("autonomous") is True:
                out.append({"ts": r.ts.isoformat(), "kind": "autonomous", "tool": r.action, "actor": r.actor,
                            "detail": f"stage {d.get('autonomy_stage')} {_safe_args(d.get('args'))}"})
    return out
