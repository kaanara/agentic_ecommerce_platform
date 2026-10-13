"""Scenario tests with scripted agents: assert graph paths, escalations and DB side effects."""

import asyncio

from sqlalchemy import select

from aecom import approvals
from aecom.bus.events import drain
from aecom.db import AgentRun, Approval, Campaign, Decision, Lesson, RegistryTool, Vendor, session
from aecom.scenarios import run_scenario
from aecom.store.adapter import get_store


def run(name):
    return asyncio.run(run_scenario(name, offline=True))


def pending_tools():
    return sorted(a["tool_name"] for a in approvals.list_approvals("pending"))


def test_bootstrap_records_decision_and_escalates_provisioning():
    run("bootstrap")
    with session() as s:
        d = s.scalars(select(Decision).where(Decision.category == "ecommerce_platform")).one()
        assert d.choice == "Lemon Squeezy"
        agents = {r.agent_id for r in s.scalars(select(AgentRun))}
    assert agents == {"researcher", "finance", "judge", "ceo"}
    assert pending_tools() == ["request_provisioning"]

    a = approvals.list_approvals("pending")[0]
    assert approvals.approve(a["id"], "tester")["status"] == "executed"
    with session() as s:
        assert s.scalars(select(Vendor).where(Vendor.name == "Lemon Squeezy")).one().status == "provisioning"


def test_product_launch_lists_active_stocked_sku():
    r = run("product-launch")
    state = r["result"]
    assert state["verdict"]["approved"] is True
    assert [t["agent"] for t in state["plan"]] == ["finance", "store_ops", "supply"]
    p = get_store().get_product("LIC-DEEPWORK-TIMER-V1")
    assert p["status"] == "active" and p["price_eur"] == 29.0
    inv = {i["sku"]: i for i in get_store().inventory()}
    assert inv["LIC-DEEPWORK-TIMER-V1"]["available_keys"] == 30


def test_campaign_spend_and_social_post_are_gated_then_executed_on_approval():
    run("campaign")
    assert pending_tools() == ["launch_campaign", "publish_social_post", "request_provisioning"]
    with session() as s:
        camp = s.scalars(select(Campaign)).one()
        assert camp.status == "draft"
    launch = next(a for a in approvals.list_approvals("pending") if a["tool_name"] == "launch_campaign")
    approvals.approve(launch["id"], "cmo-human", args_override={"budget_eur": 200})
    with session() as s:
        camp = s.scalars(select(Campaign)).one()
        assert (camp.status, camp.budget_eur) == ("live", 200)


def test_support_refund_escalates_and_resists_injection():
    r = run("support-refund")
    assert pending_tools() == ["refund_order"]
    orders = get_store().list_orders()
    assert all(o["status"] != "refunded" for o in orders)  # nothing refunded without a human
    ref = approvals.list_approvals("pending")[0]
    approvals.reject(ref["id"], "tester", "ask customer first")
    assert approvals.list_approvals("pending") == []
    assert r["last_email"] is not None


def test_supply_continuity_restocks_and_gates_new_vendor():
    run("supply-continuity")
    inv = {i["sku"]: i for i in get_store().inventory()}
    assert inv["LIC-FOCUSFLOW-PRO-V1"]["available_keys"] >= 50
    assert pending_tools() == ["register_vendor"]


def test_self_tooling_lifecycle():
    run("self-tooling")
    with session() as s:
        assert s.get(RegistryTool, "fetch").status == "sandboxed"
        assert s.get(RegistryTool, "skill-eu-vat-pricing").status == "sandboxed"
    assert pending_tools() == ["approve_tool", "approve_tool", "grant_tool", "grant_tool"]
    for a in sorted(approvals.list_approvals("pending"), key=lambda a: a["tool_name"]):  # approve before grant
        assert approvals.approve(a["id"], "cto-human")["status"] == "executed"
    drain()
    from aecom.registry.tools import allowed_tools, skills_for

    assert "fetch" in allowed_tools("researcher")
    assert any("EU VAT" in s or "VAT-inclusive" in s for s in skills_for("finance"))


def test_weekly_review_pauses_loss_making_campaign_and_proposes_lesson():
    run("weekly-review")
    with session() as s:
        camps = {c.channel: c for c in s.scalars(select(Campaign))}
        assert camps["meta_ads"].status == "paused"
        assert camps["google_ads"].status == "live"
        assert s.scalars(select(Lesson)).one().agent_id == "marketing"
    with session() as s:
        assert s.scalars(select(Approval)).first() is None
