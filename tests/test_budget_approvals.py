"""Monthly marketing budget can't be bypassed (I9); approvals are de-duplicated and expire (I10)."""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from aecom import approvals, db
from aecom.company_settings import set_setting
from aecom.db import Approval, Campaign, session
from aecom.policy import gate
from aecom.policy.gate import evaluate, run_gated
from aecom.tools.catalog import CATALOG


def _campaign(budget: float, status: str = "draft", spent: float = 0.0, created_at: datetime | None = None) -> str:
    with session() as s:
        c = Campaign(name="c", channel="google_ads", budget_eur=budget, spent_eur=spent, status=status,
                     created_at=created_at or db.now())
        s.add(c)
        s.flush()
        return c.id


def _book(campaign_id: str, amount: float, when: datetime | None = None) -> None:
    """Book ad spend to the ledger (as the simulator does), optionally back-dated."""
    from aecom import ledger
    from aecom.db import LedgerEntry, ledger_rewrites_allowed

    txn = ledger.record_expense("marketing_expense", amount, "ad spend", campaign_id)
    if when:
        with session() as s, ledger_rewrites_allowed():
            for e in s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)):
                e.created_at = when
            s.flush()


def _launch(campaign_id: str, budget: float) -> str:
    verdict = evaluate("marketing", "launch_campaign", {"campaign_id": campaign_id, "budget_eur": budget})
    if verdict.decision == "allow":
        CATALOG["launch_campaign"].fn(campaign_id=campaign_id, budget_eur=budget)
    return verdict.decision


# --------------------------------------------------------------------------- I9 budget


def test_under_budget_launch_is_allowed():
    set_setting("marketing_budget_month_eur", "300")
    c = _campaign(100, "live", spent=40)
    _book(c, 40)
    assert _launch(_campaign(0), 100) == "allow"  # 40 spent + 60 remaining + 100 = 200


def test_pause_then_relaunch_cannot_exceed_budget():
    set_setting("marketing_budget_month_eur", "200")
    a, b = _campaign(0), _campaign(0)
    assert _launch(a, 120) == "allow"
    CATALOG["pause_campaign"].fn(campaign_id=a)
    with session() as s:
        s.get(Campaign, a).spent_eur = 110.0
    _book(a, 110)
    assert _launch(b, 140) == "escalate"  # pausing doesn't hide a's 110 spent: 110 + 140 = 250 > 200
    assert _launch(b, 80) == "allow"  # 110 + 80 = 190
    assert _launch(a, 120) == "allow"  # 110 spent + 80 live remaining + 10 still unspent of a = 200
    assert _launch(_campaign(0), 20) == "escalate"  # 110 + 80 + 10 + 20 = 220 > 200


def test_relaunch_does_not_double_count_own_budget():
    set_setting("marketing_budget_month_eur", "200")
    a = _campaign(0)
    assert _launch(a, 140) == "allow"
    assert _launch(a, 140) == "allow"  # own remaining isn't counted twice


def test_last_months_live_campaign_counts_its_remaining_budget():
    set_setting("marketing_budget_month_eur", "200")
    last_month = db.now().replace(day=1) - timedelta(days=3)
    old = _campaign(140, "live", spent=20, created_at=last_month)
    _book(old, 20, last_month)  # R9: last month's spend no longer counts (was lifetime spent_eur)
    ended = _campaign(500, "ended", spent=500, created_at=last_month)  # old and finished: no longer counts
    _book(ended, 500, last_month)
    assert _launch(_campaign(0), 100) == "escalate"  # 0 this month + 120 remaining + 100 = 220
    assert _launch(_campaign(0), 70) == "allow"  # 0 + 120 + 70 = 190


def test_campaign_spanning_months_counts_only_this_months_spend():
    set_setting("marketing_budget_month_eur", "160")
    last_month = db.now().replace(day=1) - timedelta(days=3)
    c = _campaign(120, "paused", spent=120, created_at=last_month)
    _book(c, 100, last_month)
    _book(c, 20)
    # budget 160: 20 this month (not 120) + 130 = 150 allowed (lifetime would be 250); 20 + 150 = 170 escalated
    assert _launch(_campaign(0), 130) == "allow"
    assert _launch(_campaign(0), 150) == "escalate"


def test_paused_campaign_spend_this_month_still_counts():
    set_setting("marketing_budget_month_eur", "200")
    c = _campaign(100, "paused", spent=100)
    _book(c, 100)
    assert _launch(_campaign(0), 100) == "allow"
    assert _launch(_campaign(0), 101) == "escalate"


def test_relaunch_of_live_campaign_counts_once():
    set_setting("marketing_budget_month_eur", "200")
    c = _campaign(150, "live", spent=50)
    _book(c, 50)
    assert evaluate("marketing", "launch_campaign", {"campaign_id": c, "budget_eur": 150}).decision == "allow"  # 50 + 100
    assert evaluate("marketing", "launch_campaign", {"campaign_id": c, "budget_eur": 201}).decision == "escalate"


def test_vendor_budget_still_applies():
    set_setting("vendor_budget_month_eur", "30")
    assert evaluate("supply", "register_vendor", {"monthly_cost_eur": 20}).decision == "allow"
    assert evaluate("supply", "register_vendor", {"monthly_cost_eur": 40}).decision == "escalate"


# --------------------------------------------------------------------------- I10 approvals


def _escalate(args: dict) -> str:
    return asyncio.run(run_gated("marketing", "publish_social_post", args, lambda a: None, side_effect=True))


def _approval_count() -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(Approval))


def test_duplicate_escalation_returns_same_id_and_one_row():
    first = _escalate({"platform": "x", "text": "hi"})
    second = _escalate({"text": "hi", "platform": "x"})  # same args, different order
    assert _approval_count() == 1
    (a,) = approvals.list_approvals("pending")
    assert f"id={a['id']}" in first and f"id={a['id']}" in second
    assert "already pending" in second and "already pending" not in first
    assert a["expires_at"] is not None


def test_different_args_create_two_rows():
    _escalate({"platform": "x", "text": "hi"})
    _escalate({"platform": "x", "text": "hello"})
    assert _approval_count() == 2


def test_expired_approval_cannot_be_approved_and_is_hidden():
    _escalate({"platform": "x", "text": "hi"})
    (a,) = approvals.list_approvals("pending")
    with session() as s:
        s.get(Approval, a["id"]).expires_at = db.now() - timedelta(minutes=1)
    with pytest.raises(ValueError, match="approval expired - ask the agent to re-request"):
        approvals.approve(a["id"], "tester")
    with session() as s:
        assert s.get(Approval, a["id"]).status == "expired"
    assert approvals.list_approvals("pending") == []
    # a re-request after expiry creates a fresh approval
    assert "already pending" not in _escalate({"platform": "x", "text": "hi"})
    assert len(approvals.list_approvals("pending")) == 1


def test_expiry_via_clock_hides_from_pending(monkeypatch):
    _escalate({"platform": "x", "text": "hi"})
    later = db.now() + timedelta(hours=73)
    monkeypatch.setattr(approvals, "now", lambda: later)
    assert approvals.list_approvals("pending") == []
    assert [x["status"] for x in approvals.list_approvals(None)] == ["expired"]


def test_reject_allowed_while_pending():
    _escalate({"platform": "x", "text": "hi"})
    (a,) = approvals.list_approvals("pending")
    assert approvals.reject(a["id"], "tester")["status"] == "rejected"


def test_add_license_keys_allowed_while_paused():
    set_setting("agents_paused", "on")
    assert evaluate("supply", "add_license_keys", {"sku": "x", "keys": ["k"]}).decision == "allow"
    assert evaluate("support", "refund_order", {"amount_eur": 1}).decision == "deny"
    assert gate.load_policy()["defaults"]["approval_ttl_hours"] == 72
