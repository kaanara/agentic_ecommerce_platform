"""K7: safe mode - a P1 nobody reacts to makes the company safer (deliveries and replies only, no spending)."""

from datetime import timedelta

import pytest
from aecom import cli, db, incidents, monitors
from aecom.api import app as api_app
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain
from aecom.company_settings import enter_safe_mode, get_setting, leave_safe_mode, safe_mode_on, set_setting
from aecom.db import AuditLog, session
from aecom.policy.gate import SAFE_MODE_REASON, evaluate
from aecom.store.adapter import get_store
from fastapi.testclient import TestClient
from sqlalchemy import select

TOKEN = "test-token-123"


@pytest.fixture
def p1(monkeypatch):
    """A P1 monitor under test control (`p1.failing = False` makes its check pass) and recorded alerts."""
    class P1:
        failing = True
        alerts: list[dict] = []

    P1.alerts = []
    monkeypatch.setitem(monitors.MONITORS, "ledger_imbalance",
                        ("P1", lambda now: [("ledger", "ledger out of balance")] if P1.failing else []))
    monkeypatch.setattr(monitors.alerts, "route", lambda inc: P1.alerts.append(inc) or {"push": True, "email": True})
    return P1


def ledger_incident():
    return next(i for i in incidents.list_incidents() if i["monitor"] == "ledger_imbalance")


# ------------------------------------------------------------------------------------------ monitors

def test_unacknowledged_p1_for_over_2h_turns_safe_mode_on_and_alerts(p1):
    t0 = db.now()
    assert monitors.run_monitors(now=t0)["safe_mode"] is False
    r = monitors.run_monitors(now=t0 + timedelta(hours=2, minutes=1))
    inc = ledger_incident()
    assert r["safe_mode"] is True and safe_mode_on()
    assert inc["id"] in get_setting("safe_mode_reason") and "ledger_imbalance" in get_setting("safe_mode_reason")
    assert get_setting("safe_mode_since")
    extra = [a for a in p1.alerts if a["monitor"] == "safe_mode"]
    assert len(extra) == 1 and extra[0]["severity"] == "P1"
    assert extra[0]["detail"].startswith("Safe mode is ON because ") and inc["id"] in extra[0]["detail"]
    with session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "safe_mode", AuditLog.decision == "on")).first()
    monitors.run_monitors(now=t0 + timedelta(hours=3))  # already on: no second safe-mode alert
    assert len([a for a in p1.alerts if a["monitor"] == "safe_mode"]) == 1


def test_acknowledged_p1_does_not_turn_safe_mode_on(p1):
    t0 = db.now()
    monitors.run_monitors(now=t0)
    incidents.acknowledge(ledger_incident()["id"], "kaan")
    assert monitors.run_monitors(now=t0 + timedelta(hours=5))["safe_mode"] is False
    assert not safe_mode_on() and not [a for a in p1.alerts if a["monitor"] == "safe_mode"]


def test_p1_only_1h_old_does_not_turn_safe_mode_on(p1):
    t0 = db.now()
    monitors.run_monitors(now=t0)
    monitors.run_monitors(now=t0 + timedelta(hours=1))
    monitors.run_monitors(now=t0 + timedelta(hours=2))  # exactly 2 h is not "more than"
    assert not safe_mode_on()


def test_monitors_never_leave_safe_mode_a_human_does(p1, capsys):
    t0 = db.now()
    monitors.run_monitors(now=t0)
    monitors.run_monitors(now=t0 + timedelta(hours=2, minutes=1))
    p1.failing = False  # the problem goes away and the incident auto-resolves
    monitors.run_monitors(now=t0 + timedelta(hours=3))
    assert ledger_incident()["status"] == "resolved" and safe_mode_on()

    with pytest.raises(ValueError):
        leave_safe_mode("  ")
    with pytest.raises(SystemExit):
        cli.main(["safe-mode", "off"])
    assert safe_mode_on()
    assert leave_safe_mode("kaan") is True and not safe_mode_on() and not get_setting("safe_mode_reason")
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "safe_mode", AuditLog.decision == "off")).one()
        assert row.actor == "human:kaan"


# ------------------------------------------------------------------------------------------ gate

def test_safe_mode_allows_deliveries_and_replies_but_no_spending():
    enter_safe_mode("test")
    for agent, tool in [("store_ops", "deliver_order"), ("supply", "add_license_keys"), ("support", "reply_email")]:
        assert evaluate(agent, tool, {"order_id": "x"}).decision == "allow", tool
    for agent, tool, args in [("marketing", "launch_campaign", {"budget_eur": 1}), ("support", "refund_order", {"amount_eur": 1}),
                              ("finance", "record_expense", {"amount_eur": 1}), ("supply", "register_vendor", {}),
                              ("store_ops", "update_price", {"sku": "x", "price_eur": 1})]:
        verdict = evaluate(agent, tool, args)
        assert verdict.decision == "deny" and verdict.reason == SAFE_MODE_REASON, tool
    assert evaluate("support", "list_products", {}, side_effect=False).decision == "allow"
    # send_email continues but still only to the customer of the current case
    assert evaluate("support", "send_email", {"to": "someone@else.com", "subject": "s", "body": "b"}).decision == "escalate"


def test_kill_switch_still_denies_deliveries_in_safe_mode():
    enter_safe_mode("test")
    set_setting("kill_switch", "on")
    verdict = evaluate("store_ops", "deliver_order", {"order_id": "x"})
    assert verdict.decision == "deny" and "Kill switch" in verdict.reason


def test_paid_order_is_delivered_in_safe_mode(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    enter_safe_mode("test")
    order = get_store().create_order("buyer@example.com", "LIC-FOCUSFLOW-PRO-V1", "DE")
    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"


# ------------------------------------------------------------------------------------------ CLI / API

def test_cli_safe_mode_status_and_off(capsys):
    cli.main(["safe-mode"])
    assert '"safe_mode": false' in capsys.readouterr().out
    enter_safe_mode("P1 incident inc_1 open")
    cli.main(["safe-mode", "status"])
    out = capsys.readouterr().out
    assert '"safe_mode": true' in out and "inc_1" in out
    cli.main(["safe-mode", "off", "--by", "kaan"])
    assert capsys.readouterr().out.strip() == "safe mode off" and not safe_mode_on()


def test_api_safe_mode_route_is_protected_and_health_shows_it(monkeypatch):
    monkeypatch.setattr(api_app.secrets, "get", lambda name: TOKEN if name == "AECOM_API_TOKEN" else None)
    client = TestClient(api_app.app)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/health").json()["safe_mode"] is False
    enter_safe_mode("test")
    body = client.get("/health").json()
    assert body["safe_mode"] is True and {"ok", "db", "kill_switch", "agents_paused", "open_incidents", "event_bus"} <= set(body)

    assert client.post("/safe-mode", params={"on": False}).status_code == 401
    r = client.post("/safe-mode", params={"on": False, "by": "kaan"}, headers=auth)
    assert r.status_code == 200 and r.json() == {"safe_mode": False}
    assert client.get("/health").json()["safe_mode"] is False
    assert client.post("/safe-mode", params={"on": True}, headers=auth).json() == {"safe_mode": True}
