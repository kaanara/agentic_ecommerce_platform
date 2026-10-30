"""J14: quarterly look back at past decision records (ADRs) - did each one deliver what it promised?

``due()`` lists accepted decisions at least 60 days old that have not yet been reviewed this quarter.
``evidence()`` gathers small, factual, read-only numbers (never invented) from ``aecom.metrics``,
``aecom.trust.real_sales`` and vendor status to help a human judge the outcome. ``record_review()`` is the only
way a review is written - it always needs a human name (``by``), and a note unless the outcome is ``delivered``.

No agent tool calls into this module (see ``tools/catalog.py``): a quarterly verdict on the company's own past
decisions is a human judgement call, not something an agent marks for itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from aecom import company_settings, metrics, trust
from aecom.audit import audit
from aecom.db import Decision, DecisionReview, Vendor, session
from aecom.db import now as db_now

OUTCOMES = ("delivered", "partly", "not_delivered", "superseded")
DUE_AFTER_DAYS = 60
_REMINDER_SETTING_KEY = "decision_review_reminder_quarter"
# Categories whose decisions touch numbers we can read back from the books (metrics/ledger/trust/vendors).
# The rest (social, mcp, ...) get the same evidence shell but an empty `facts`: no number exists to report.
_NUMERIC_CATEGORIES = ("strategy", "ecommerce_platform", "ad_platform", "payment", "vendor")


def current_quarter(now: datetime | None = None) -> str:
    """'YYYY-Qn' for ``now`` (or the real current time)."""
    t = now or db_now()
    return f"{t.year}-Q{(t.month - 1) // 3 + 1}"


def _decision_dict(d: Decision, at: datetime) -> dict[str, Any]:
    return {"id": d.id, "category": d.category, "title": d.title, "choice": d.choice, "status": d.status,
            "decided_by": d.decided_by, "created_at": d.created_at, "days_old": (at - d.created_at).days}


def due(now: datetime | None = None) -> list[dict[str, Any]]:
    """Accepted decisions at least 60 days old with no review row for the current quarter, oldest first."""
    t = now or db_now()
    quarter = current_quarter(t)
    cutoff = t - timedelta(days=DUE_AFTER_DAYS)
    with session() as s:
        decisions = list(s.scalars(select(Decision).where(Decision.status == "accepted", Decision.created_at <= cutoff)
                                   .order_by(Decision.created_at)))
        reviewed_ids = set(s.scalars(select(DecisionReview.decision_id).where(DecisionReview.quarter == quarter)))
        return [_decision_dict(d, t) for d in decisions if d.id not in reviewed_ids]


def _vendor_facts(choice: str) -> dict[str, Any]:
    with session() as s:
        v = s.scalars(select(Vendor).where(func.lower(Vendor.name) == choice.strip().lower())).first()
    if v is None:
        return {}
    return {"vendor_status": v.status, "monthly_cost_eur": v.monthly_cost_eur, "verified_at": v.verified_at,
            "last_check_error": v.last_check_error}


def _facts(category: str, choice: str, decided_at: datetime) -> dict[str, Any]:
    """Small, factual current figures for categories where numbers exist. Never invents: a category or vendor
    with nothing to show simply gets an empty dict."""
    if category == "strategy":
        p = metrics.pnl()  # lifetime company snapshot - a strategy call is judged on the whole business
        return {"net_revenue_eur_lifetime": p["net_revenue"], "net_profit_eur_lifetime": p["net_profit"],
                "gross_margin_pct_lifetime": p["gross_margin_pct"], "real_paid_orders_lifetime": trust.real_sales()}
    if category == "ecommerce_platform":
        k = metrics.kpis(start=decided_at)
        return {"orders_since_decision": k["orders"], "net_revenue_eur_since_decision": k["net_revenue_eur"],
                "aov_eur_since_decision": k["aov_eur"], "real_paid_orders_lifetime": trust.real_sales()}
    if category == "ad_platform":
        k = metrics.kpis(start=decided_at)
        return {"net_revenue_eur_since_decision": k["net_revenue_eur"], "roas_since_decision": k["roas"],
                "cac_eur_since_decision": k["cac_eur"]}
    if category == "payment":
        b = metrics.balances(start=decided_at)
        return {"revenue_eur_since_decision": round(b.get("revenue", 0.0), 2),
                "refunds_eur_since_decision": round(b.get("refunds", 0.0), 2),
                "real_paid_orders_lifetime": trust.real_sales()}
    if category == "vendor":
        return _vendor_facts(choice)
    return {}


def evidence(decision_id: str) -> dict[str, Any]:
    """Read-only facts to help a human judge one decision: its text, category, age, and (where numbers exist for
    its category) current figures from the books. Never invents anything beyond what is already stored."""
    t = db_now()
    with session() as s:
        d = s.get(Decision, decision_id)
        if d is None:
            raise ValueError(f"unknown decision {decision_id}")
        out = {"id": d.id, "category": d.category, "title": d.title, "choice": d.choice, "rationale": d.rationale,
              "status": d.status, "decided_by": d.decided_by, "decided_at": d.created_at,
              "days_old": (t - d.created_at).days}
    out["facts"] = _facts(d.category, d.choice, d.created_at)
    if not out["facts"] and d.category not in _NUMERIC_CATEGORIES:
        out["facts_note"] = f"No standard figures are tracked for category '{d.category}'."
    elif not out["facts"] and d.category == "vendor":
        out["facts_note"] = f"No vendor record named '{d.choice}' was found."
    return out


def record_review(decision_id: str, outcome: str, note: str, by: str) -> dict[str, Any]:
    """Record (or update) this quarter's verdict for a decision. ``by`` is always required; ``note`` is required
    unless the outcome is 'delivered'. Upserts the (decision_id, quarter) row and audits as human:<by>."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {', '.join(OUTCOMES)}")
    if not (by or "").strip():
        raise ValueError("a reviewer name is required (--by NAME)")
    if outcome != "delivered" and not (note or "").strip():
        raise ValueError(f"a note is required when the outcome is '{outcome}'")
    quarter = current_quarter()
    with session() as s:
        if s.get(Decision, decision_id) is None:
            raise ValueError(f"unknown decision {decision_id}")
        row = s.scalars(select(DecisionReview).where(DecisionReview.decision_id == decision_id,
                                                     DecisionReview.quarter == quarter)).first()
        if row is None:
            row = DecisionReview(decision_id=decision_id, quarter=quarter, outcome=outcome, note=note, reviewed_by=by)
            s.add(row)
        else:
            row.outcome, row.note, row.reviewed_by, row.reviewed_at = outcome, note, by, db_now()
        s.flush()
        out = _review_dict(row)
    audit(f"human:{by}", "decision_review", outcome, {"decision_id": decision_id, "quarter": quarter, "note": note})
    return out


def _review_dict(r: DecisionReview) -> dict[str, Any]:
    return {"id": r.id, "decision_id": r.decision_id, "quarter": r.quarter, "outcome": r.outcome, "note": r.note,
            "reviewed_by": r.reviewed_by, "reviewed_at": r.reviewed_at}


def list_reviews(decision_id: str | None = None) -> list[dict[str, Any]]:
    """Past reviews, most recent first; optionally for one decision."""
    with session() as s:
        q = select(DecisionReview)
        if decision_id:
            q = q.where(DecisionReview.decision_id == decision_id)
        q = q.order_by(DecisionReview.reviewed_at.desc())
        return [_review_dict(r) for r in s.scalars(q)]


def remind_due_reviews(now: datetime | None = None) -> dict[str, Any]:
    """Once per quarter: if any decision is due its review, send ONE owner notification naming how many and where
    to review them. Idempotent per quarter via company_settings (a second scheduler tick is a no-op). Never raises."""
    try:
        t = now or db_now()
        quarter = current_quarter(t)
        if company_settings.get_setting(_REMINDER_SETTING_KEY) == quarter:
            return {"sent": False, "quarter": quarter, "reason": "already reminded this quarter"}
        pending = due(t)
        if not pending:
            return {"sent": False, "quarter": quarter, "reason": "none due"}
        text = (f"{len(pending)} decision record(s) are due their quarterly review. "
                "Review them in the dashboard under Accounting > Decision review.")
        from aecom import notify

        notify.route({"kind": "message", "text": text, "urgency": "info"})
        company_settings.set_setting(_REMINDER_SETTING_KEY, quarter)
        return {"sent": True, "quarter": quarter, "count": len(pending)}
    except Exception:  # noqa: BLE001 - a reminder must never break the scheduler tick that runs it
        return {"sent": False, "reason": "error"}
