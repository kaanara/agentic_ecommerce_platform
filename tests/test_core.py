import pytest
from sqlalchemy import select

from aecom import ledger
from aecom.bootstrap import seed_demo
from aecom.bus.events import drain
from aecom.company_settings import set_setting
from aecom.db import AuditLog, OutboxEmail, session
from aecom.persona.loader import agent_ids, load_spec
from aecom.policy.gate import evaluate
from aecom.store.adapter import get_store, vat_included
from aecom.tools.catalog import CATALOG

EXPECTED = {"ceo": "opus", "researcher": "sonnet", "judge": "sonnet", "foreman": "sonnet", "finance": "sonnet",
            "store_ops": "haiku", "marketing": "haiku", "support": "haiku", "supply": "haiku",
            "sentinel": "sonnet", "engineer": "sonnet"}


def test_all_agents_load_with_expected_tiers_and_known_tools():
    assert set(agent_ids()) == set(EXPECTED)
    for agent_id, tier in EXPECTED.items():
        spec = load_spec(agent_id)
        assert spec.model_tier == tier
        assert spec.soul and spec.procedures
        assert set(spec.tools) <= set(CATALOG), set(spec.tools) - set(CATALOG)


@pytest.mark.parametrize("agent,tool,args,expected", [
    ("support", "refund_order", {"amount_eur": 20}, "allow"),
    ("support", "refund_order", {"amount_eur": 80}, "escalate"),
    ("marketing", "refund_order", {"amount_eur": 5}, "deny"),
    ("marketing", "launch_campaign", {"campaign_id": "x", "budget_eur": 100}, "allow"),
    ("marketing", "launch_campaign", {"campaign_id": "x", "budget_eur": 300}, "escalate"),
    ("marketing", "publish_social_post", {"platform": "x", "text": "hi"}, "escalate"),
    ("ceo", "request_provisioning", {}, "escalate"),
    ("foreman", "approve_tool", {"name": "x"}, "escalate"),
    ("ceo", "change_policy", {}, "deny"),
    ("researcher", "list_products", {}, "allow"),
    ("anyone", "mcp:unknown/write", {}, "escalate"),
])
def test_policy_matrix(agent, tool, args, expected):
    assert evaluate(agent, tool, args).decision == expected


def test_price_change_percentage_rule():
    seed_demo()
    assert evaluate("store_ops", "update_price", {"sku": "LIC-FOCUSFLOW-PRO-V1", "price_eur": 52}).decision == "allow"
    assert evaluate("store_ops", "update_price", {"sku": "LIC-FOCUSFLOW-PRO-V1", "price_eur": 79}).decision == "escalate"


def test_kill_switch_blocks_side_effects_but_not_reads():
    set_setting("kill_switch", "on")
    assert evaluate("support", "refund_order", {"amount_eur": 1}).decision == "deny"
    assert evaluate("support", "list_products", {}, side_effect=False).decision == "allow"


def test_order_delivery_ledger_and_email():
    seed_demo()
    order = get_store().create_order("a@example.com", "LIC-FOCUSFLOW-PRO-V1", "FR")
    assert order["vat_eur"] == vat_included(49.0, "FR") == 8.17
    drain()
    o = get_store().get_order(order["id"])
    assert o["status"] == "delivered"
    with session() as s:
        assert s.scalars(select(OutboxEmail).where(OutboxEmail.to == "a@example.com")).first() is not None
    b = ledger.balances()
    assert abs(sum(v for k, v in b.items() if k in ledger.DEBIT_NORMAL) - sum(v for k, v in b.items() if k not in ledger.DEBIT_NORMAL)) < 1e-6
    assert ledger.pnl()["net_revenue"] == pytest.approx(49.0 - 8.17)


def test_ledger_rejects_unbalanced():
    with pytest.raises(ValueError):
        ledger.post([("cash", 10, 0), ("revenue", 0, 9)], "bad")


def test_audit_log_is_append_only():
    from aecom.audit import audit

    audit("system", "x", "info")
    with pytest.raises(PermissionError):
        with session() as s:
            row = s.scalars(select(AuditLog)).first()
            row.decision = "tampered"


def test_out_of_stock_triggers_restock_and_redelivery():
    seed_demo()
    store = get_store()
    store.create_product("LIC-TINY-V1", "Tiny", 10.0, "license")
    CATALOG["add_license_keys"].fn(sku="LIC-TINY-V1", count=1)
    store.set_status("LIC-TINY-V1", "active")
    first = store.create_order("x@example.com", "LIC-TINY-V1")
    second = store.create_order("y@example.com", "LIC-TINY-V1")
    drain()
    assert store.get_order(first["id"])["status"] == "delivered"
    assert store.get_order(second["id"])["status"] == "delivered"  # restocked by Supply reflex, then re-delivered
