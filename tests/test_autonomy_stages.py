"""Phase 5 / G5: per-action autonomy stages in the gate."""

import asyncio

import pytest
from sqlalchemy import select

from aecom import autonomy
from aecom.bootstrap import seed_demo
from aecom.company_settings import set_setting
from aecom.context import current_counterparty
from aecom.db import AuditLog, session
from aecom.policy.gate import evaluate, load_policy, run_gated
from aecom.tools.catalog import CATALOG

SKU = "LIC-FOCUSFLOW-PRO-V1"
EVID = {"real_sales": 12, "approvals": 8, "approve_unchanged_rate": 0.95}

# (agent, tool, arg name, stage1 limit, stage2 limit, stage3 limit)
CASES = [
    ("support", "refund_order", "amount_eur", 50, 150, 500),
    ("marketing", "launch_campaign", "budget_eur", 150, 300, 600),
    ("finance", "record_expense", "amount_eur", 200, 400, 800),
    ("supply", "register_vendor", "monthly_cost_eur", 50, 100, 200),
]


def _args(tool, arg, value):
    args = {arg: value}
    if tool == "launch_campaign":
        args["campaign_id"] = "c1"
    return args


def _stage(tool, n):
    set_setting(autonomy.STAGE_KEY.format(tool=tool), str(n))


def _audits(action="autonomy_stage"):
    with session() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


@pytest.mark.parametrize("agent,tool,arg,l1,l2,l3", CASES)
def test_stage_1_is_todays_behaviour(agent, tool, arg, l1, l2, l3):
    r = evaluate(agent, tool, _args(tool, arg, l1))
    assert (r.decision, r.autonomy_stage, r.autonomous) == ("allow", 1, False)
    assert evaluate(agent, tool, _args(tool, arg, l1 + 1)).decision == "escalate"


@pytest.mark.parametrize("agent,tool,arg,l1,l2,l3", CASES)
def test_stage_2_and_3_allow_between_limits_as_autonomous(agent, tool, arg, l1, l2, l3):
    _stage(tool, 2)
    r = evaluate(agent, tool, _args(tool, arg, l1 + 1))
    assert (r.decision, r.autonomy_stage, r.autonomous) == ("allow", 2, True)
    assert evaluate(agent, tool, _args(tool, arg, l1)).autonomous is False  # would have passed at stage 1 anyway
    assert evaluate(agent, tool, _args(tool, arg, l2 + 1)).decision == "escalate"
    _stage(tool, 3)
    r = evaluate(agent, tool, _args(tool, arg, l2 + 1))
    assert (r.decision, r.autonomy_stage, r.autonomous) == ("allow", 3, True)
    assert evaluate(agent, tool, _args(tool, arg, l3 + 1)).decision == "escalate"


def test_update_price_stages():
    seed_demo()  # FocusFlow Pro costs 49
    assert evaluate("store_ops", "update_price", {"sku": SKU, "price_eur": 62}).decision == "escalate"  # +26%
    _stage("update_price", 2)
    r = evaluate("store_ops", "update_price", {"sku": SKU, "price_eur": 62})
    assert (r.decision, r.autonomous, r.autonomy_stage) == ("allow", True, 2)
    assert evaluate("store_ops", "update_price", {"sku": SKU, "price_eur": 79}).decision == "escalate"  # +61%
    _stage("update_price", 3)
    assert evaluate("store_ops", "update_price", {"sku": SKU, "price_eur": 70}).decision == "allow"  # +43%
    assert evaluate("store_ops", "update_price", {"sku": SKU, "price_eur": 79}).decision == "escalate"


def test_allowed_call_audit_carries_stage_and_autonomous():
    _stage("refund_order", 2)
    msg = asyncio.run(run_gated("support", "refund_order", {"amount_eur": 100}, lambda a: "ok", side_effect=True))
    assert msg == "ok"
    detail = _audits("refund_order")[-1].detail
    assert (detail["autonomy_stage"], detail["autonomous"]) == (2, True)


def test_monthly_budget_is_a_hard_cap_at_stage_3():
    _stage("launch_campaign", 3)
    set_setting("marketing_budget_month_eur", "400")
    assert evaluate("marketing", "launch_campaign", {"campaign_id": "c1", "budget_eur": 400}).decision == "allow"
    r = evaluate("marketing", "launch_campaign", {"campaign_id": "c1", "budget_eur": 500})
    assert r.decision == "escalate" and "marketing_budget_month_eur" in r.reason
    set_setting("vendor_budget_month_eur", "60")
    _stage("register_vendor", 3)
    assert evaluate("supply", "register_vendor", {"monthly_cost_eur": 150}).decision == "escalate"


def test_recipient_rule_and_t3_unaffected_at_any_stage():
    for tool, *_ in CASES:
        _stage(tool, 3)
    _stage("send_email", 3)  # not promotable: ignored
    assert autonomy.current_stage("send_email") == 1
    token = current_counterparty.set("a@example.com")
    try:
        assert evaluate("support", "send_email", {"to": "b@example.com"}).decision == "escalate"
        assert evaluate("support", "send_email", {"to": "a@example.com"}).decision == "allow"
    finally:
        current_counterparty.reset(token)
    for agent, tool in [("marketing", "send_newsletter"), ("ceo", "request_provisioning"), ("foreman", "approve_tool")]:
        _stage(tool, 3)
        assert evaluate(agent, tool, {}).decision == "escalate"
    assert evaluate("ceo", "change_policy", {}).decision == "deny"


def test_only_the_five_rules_have_stages_and_only_allowed_keys():
    rules = load_policy()["rules"]
    staged = {t for t, r in rules.items() if r.get("stages")}
    assert staged == {"refund_order", "update_price", "launch_campaign", "record_expense", "register_vendor"}
    for t in staged:
        assert rules[t]["tier"] == "T2"
        for stage in rules[t]["stages"].values():
            assert set(stage) <= {"limits", "price_change_pct_max", "notify"}


def test_effective_rule_ignores_other_keys(monkeypatch):
    policy = {"rules": {"x": {"tier": "T2", "limits": {"a": 1, "b": 2}, "monthly_budget_setting": "k", "stages": {
        2: {"limits": {"a": 5}, "tier": "T0", "monthly_budget_setting": "z", "agents": ["q"], "notify": True}}}}}
    monkeypatch.setattr("aecom.policy.gate.load_policy", lambda: policy)
    _stage("x", 2)
    eff = autonomy.effective_rule("x", policy["rules"]["x"])
    assert eff["limits"] == {"a": 5, "b": 2} and eff["notify"] is True
    assert (eff["tier"], eff["monthly_budget_setting"], "agents" in eff) == ("T2", "k", False)


def test_invalid_setting_values_fall_back_to_one_and_high_is_clamped():
    for bad in ("", "abc", "0", "-3", "1.5"):
        _stage("refund_order", bad)
        assert autonomy.current_stage("refund_order") == 1
    _stage("refund_order", 9)
    assert autonomy.current_stage("refund_order") == 3
    assert autonomy.defined_stages("refund_order") == [1, 2, 3]
    assert autonomy.defined_stages("send_email") == [1]


def test_set_stage_rules_and_audit():
    with pytest.raises(ValueError):
        autonomy.set_stage("refund_order", 2, "kaan", "good")  # no evidence
    with pytest.raises(ValueError):
        autonomy.set_stage("refund_order", 2, "kaan", "good", evidence={})
    with pytest.raises(ValueError):
        autonomy.set_stage("refund_order", 2, "", "good", evidence=EVID)  # nobody named
    with pytest.raises(ValueError):
        autonomy.set_stage("refund_order", 4, "kaan", evidence=EVID)  # undefined
    with pytest.raises(ValueError):
        autonomy.set_stage("send_email", 2, "kaan", evidence=EVID)  # unpromotable
    with pytest.raises(ValueError):
        autonomy.set_stage("change_policy", 2, "kaan", evidence=EVID)
    assert autonomy.current_stage("refund_order") == 1 and not _audits()

    autonomy.set_stage("refund_order", 2, "kaan", "10 sales", evidence=EVID)
    assert autonomy.current_stage("refund_order") == 2 and autonomy.promoted_tools() == {"refund_order": 2}
    a = _audits()[-1]
    assert (a.actor, a.decision) == ("human:kaan", "promoted")
    assert a.detail == {"tool": "refund_order", "from": 1, "to": 2, "reason": "10 sales", "evidence": EVID}

    autonomy.set_stage("refund_order", 3, "kaan", evidence=EVID)
    autonomy.set_stage("refund_order", 2, "kaan", "wobbly")  # decrease needs no evidence
    a = _audits()[-1]
    assert (a.decision, a.detail["from"], a.detail["to"]) == ("demoted", 3, 2)


def test_system_demote_and_noop():
    assert autonomy.demote("refund_order", "nothing to do") == 1
    assert _audits() == []
    autonomy.set_stage("refund_order", 3, "kaan", evidence=EVID)
    autonomy.demote("refund_order", "refund went wrong")
    assert autonomy.current_stage("refund_order") == 1 and autonomy.promoted_tools() == {}
    a = _audits()[-1]
    assert (a.actor, a.decision, a.detail["reason"]) == ("system", "demoted", "refund went wrong")
    assert evaluate("support", "refund_order", {"amount_eur": 80}).decision == "escalate"


def test_no_agent_tool_changes_a_stage():
    assert not [n for n in CATALOG if "stage" in n or "autonomy" in n or "promote" in n]
    assert load_policy()["rules"]["change_policy"]["tier"] == "T4"
