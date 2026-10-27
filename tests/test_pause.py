"""Pause agents (deliveries continue) vs. the kill switch (stop everything) (A4)."""

from datetime import timedelta

from fastapi.testclient import TestClient

from aecom.api import app as api_app
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain
from aecom.company_settings import set_setting
from aecom.policy.gate import evaluate
from aecom.store.adapter import get_store

TOKEN = "test-token-123"
CUSTOMER_EMAIL = {"to": "a@example.com", "subject": "hi", "body": "hello"}


def test_paused_denies_agent_actions_but_not_delivery():
    set_setting("agents_paused", "on")
    for agent, tool, args in [("support", "refund_order", {"amount_eur": 1}), ("support", "send_email", CUSTOMER_EMAIL)]:
        verdict = evaluate(agent, tool, args)
        assert verdict.decision == "deny" and verdict.reason == "Agents are paused - only order delivery continues."
    assert evaluate("store_ops", "deliver_order", {"order_id": "x"}).decision == "allow"
    assert evaluate("support", "list_products", {}, side_effect=False).decision == "allow"


def test_kill_switch_wins_over_pause():
    set_setting("agents_paused", "on")
    set_setting("kill_switch", "on")
    verdict = evaluate("store_ops", "deliver_order", {"order_id": "x"})
    assert verdict.decision == "deny" and "Kill switch" in verdict.reason


def test_both_off_is_normal():
    set_setting("agents_paused", "off")
    set_setting("kill_switch", "off")
    assert evaluate("support", "refund_order", {"amount_eur": 1}).decision == "allow"
    assert evaluate("store_ops", "deliver_order", {"order_id": "x"}).decision == "allow"


def test_health_shows_both_flags_and_pause_route_is_protected(monkeypatch):
    monkeypatch.setattr(api_app.secrets, "get", lambda name: TOKEN if name == "AECOM_API_TOKEN" else None)
    client = TestClient(api_app.app)
    flags = lambda: {k: client.get("/health").json()[k] for k in ("ok", "kill_switch", "agents_paused")}  # noqa: E731
    assert flags() == {"ok": True, "kill_switch": False, "agents_paused": False}

    assert client.post("/agents-pause", params={"on": True}).status_code == 401
    r = client.post("/agents-pause", params={"on": True}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json() == {"agents_paused": True}
    assert flags() == {"ok": True, "kill_switch": False, "agents_paused": True}


def test_paid_order_is_delivered_while_paused(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    set_setting("agents_paused", "on")
    order = get_store().create_order("buyer@example.com", "LIC-FOCUSFLOW-PRO-V1", "DE")
    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"



def test_switch_changes_are_audited_for_the_governance_report():
    from sqlalchemy import select

    from aecom.company_settings import set_flag
    from aecom.db import AuditLog, session

    set_flag("kill_switch", True, "kaan")
    set_flag("agents_paused", False, "dashboard")
    with session() as s:
        rows = [(a.actor, a.action, a.decision) for a in s.scalars(select(AuditLog).where(AuditLog.action.in_(["kill_switch", "agents_paused"])))]
    assert ("human:kaan", "kill_switch", "on") in rows and ("human:dashboard", "agents_paused", "off") in rows
