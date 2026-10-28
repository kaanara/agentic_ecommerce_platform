"""Phase 5 / G6: automatic demotion and abuse detection."""

from datetime import timedelta

import pytest
from aecom import autonomy, autonomy_guard, db, incidents, metrics, monitors
from aecom.audit import audit
from aecom.autonomy_guard import check_and_demote
from aecom.bootstrap import seed_demo
from aecom.chaos import run_scenario
from aecom.chaos.invariants import no_unearned_promotion
from aecom.company_settings import enter_safe_mode, set_flag, set_setting
from aecom.db import AuditLog, Customer, ErrorRow, Order, Product, Refund, session
from sqlalchemy import delete, select

EVID = {"real_sales": 12, "approvals": 8, "approve_unchanged_rate": 0.95}
TOOLS = ["refund_order", "update_price", "launch_campaign", "record_expense", "register_vendor"]


@pytest.fixture(autouse=True)
def quiet_monitors(monkeypatch):
    monkeypatch.setattr(monitors, "AUTO_DIAGNOSE", False)
    monkeypatch.setattr(monitors.alerts, "route", lambda *a, **k: None)
    monkeypatch.setattr(monitors.alerts, "heartbeat", lambda *a, **k: None)


def promote(*tools):
    for t in tools or TOOLS:
        autonomy.set_stage(t, 2, "kaan", "test", evidence=EVID)


def stages():
    return {t: autonomy.current_stage(t) for t in TOOLS}


def make_orders(n, *, age=timedelta(hours=1), customer_id=None):
    """n delivered orders, one new customer each unless ``customer_id`` is given; returns the order ids."""
    seed_demo()
    ids = []
    with session() as s:
        pid = s.scalars(select(Product.id)).first()
        for i in range(n):
            cid = customer_id
            if cid is None:
                c = Customer(email=f"g6-{i}@example.com", country="DE")
                s.add(c)
                s.flush()
                cid = c.id
            o = Order(customer_id=cid, product_id=pid, amount_eur=10.0, status="delivered", created_at=db.now() - age)
            s.add(o)
            s.flush()
            ids.append(o.id)
    return ids


def refund(order_ids, created_at=None):
    with session() as s:
        for oid in order_ids:
            s.add(Refund(order_id=oid, amount_eur=1.0, reason="t", created_at=created_at or db.now()))


def demotions():
    with session() as s:
        return [r.detail["tool"] for r in s.scalars(select(AuditLog).where(
            AuditLog.action == "autonomy_stage", AuditLog.decision == "demoted").order_by(AuditLog.id))]


def incident_subjects():
    return sorted(i["subject"] for i in incidents.list_incidents() if i["monitor"] == "autonomy_demotion")


def deny(actor, n, ts=None):
    with session() as s:
        for _ in range(n):
            s.add(AuditLog(actor=actor, action="refund_order", decision="deny", detail={}, ts=ts or db.now()))


def open_for(monitor):
    return [(i["subject"], i["severity"]) for i in incidents.list_incidents(status="open") if i["monitor"] == monitor]


# ----------------------------------------------------------------------------------------------- containment
def test_nothing_promoted_nothing_happens():
    set_flag("kill_switch", True, "kaan")
    assert check_and_demote() == [] and demotions() == [] and incident_subjects() == []


def test_safe_mode_demotes_all_promoted_tools():
    promote()
    assert check_and_demote() == []
    enter_safe_mode("test")
    done = check_and_demote()
    assert sorted(d["tool"] for d in done) == sorted(TOOLS) and set(stages().values()) == {1}
    assert incident_subjects() == sorted(TOOLS)


def test_kill_switch_switched_on_recently_demotes_even_after_it_is_off():
    promote("refund_order", "update_price")
    set_flag("kill_switch", True, "kaan")
    set_flag("kill_switch", False, "kaan")  # off again, but it was on within 24 h
    assert {d["tool"] for d in check_and_demote()} == {"refund_order", "update_price"}


def test_kill_switch_older_than_24h_does_not_count():
    promote("refund_order")
    with session() as s:
        s.add(AuditLog(actor="human:kaan", action="kill_switch", decision="on", detail={},
                       ts=db.now() - timedelta(hours=25)))
    assert check_and_demote() == [] and autonomy.current_stage("refund_order") == 2


def test_open_or_acknowledged_p1_demotes_all_but_p2_and_resolved_do_not():
    promote()
    incidents.raise_incident("m", "P2", "a", "d")
    inc, _ = incidents.raise_incident("m", "P1", "b", "d")
    incidents.resolve(inc["id"], "kaan")
    assert check_and_demote() == []
    inc, _ = incidents.raise_incident("m", "P1", "c", "d")
    incidents.acknowledge(inc["id"], "kaan")
    assert len(check_and_demote()) == len(TOOLS)


# ----------------------------------------------------------------------------------------------- refund abuse
def test_refund_rate_threshold():
    promote("refund_order", "record_expense")
    ids = make_orders(20)
    refund(ids[:2])  # 10%: not above
    assert check_and_demote() == [] and autonomy.current_stage("refund_order") == 2
    refund(ids[2:3])  # 15%
    done = check_and_demote()
    assert [d["tool"] for d in done] == ["refund_order"] and autonomy.current_stage("record_expense") == 2


def test_refund_rate_needs_ten_orders():
    promote("refund_order")
    ids = make_orders(9)
    refund(ids[:5])
    assert check_and_demote() == []


def test_refund_rate_ignores_orders_older_than_7_days():
    promote("refund_order")
    ids = make_orders(12, age=timedelta(days=8))
    refund(ids[:6], created_at=db.now() - timedelta(days=8))
    assert check_and_demote() == []


def test_customer_with_three_refunds_in_30_days():
    promote("refund_order")
    seed_demo()
    with session() as s:
        c = Customer(email="serial@example.com", country="DE")
        s.add(c)
        s.flush()
        cid = c.id
    ids = make_orders(3, age=timedelta(days=20), customer_id=cid)
    refund(ids[:2], created_at=db.now() - timedelta(days=10))
    assert check_and_demote() == []  # two refunds are fine
    refund(ids[2:], created_at=db.now() - timedelta(days=10))
    assert [d["tool"] for d in check_and_demote()] == ["refund_order"]


# ----------------------------------------------------------------------------------------------- deny spike
def test_deny_spike_threshold_and_window():
    promote("refund_order", "launch_campaign")
    deny("support", 19)
    deny("marketing", 30, ts=db.now() - timedelta(hours=2))  # too old
    assert check_and_demote() == []
    deny("support", 1)
    assert {d["tool"] for d in check_and_demote()} == {"refund_order", "launch_campaign"}


def test_deny_spike_is_per_actor():
    promote("refund_order")
    deny("support", 10)
    deny("finance", 10)
    assert check_and_demote() == []


# ----------------------------------------------------------------------------------------------- KPI drops
@pytest.fixture
def fake_kpis(monkeypatch):
    state = {"roas": 2.0, "spend": 500.0, "margin": 80.0, "orders": 50}
    monkeypatch.setattr(metrics, "pnl", lambda s=None, e=None: {"marketing_expense": state["spend"]})
    monkeypatch.setattr(metrics, "kpis", lambda s=None, e=None: {
        "roas": state["roas"], "orders": state["orders"], "gross_margin_pct": state["margin"]})
    return state


def test_roas_drop(fake_kpis):
    promote()
    assert check_and_demote() == []
    fake_kpis["roas"] = 1.0  # not below
    assert check_and_demote() == []
    fake_kpis["spend"], fake_kpis["roas"] = 99.0, 0.2  # too little spend to judge
    assert check_and_demote() == []
    fake_kpis["spend"] = 100.0
    assert [d["tool"] for d in check_and_demote()] == ["launch_campaign"]
    assert autonomy.current_stage("update_price") == 2


def test_gross_margin_drop(fake_kpis):
    promote()
    fake_kpis["margin"], fake_kpis["orders"] = 10.0, 9
    assert check_and_demote() == []
    fake_kpis["margin"], fake_kpis["orders"] = 50.0, 10
    assert check_and_demote() == []
    fake_kpis["margin"] = 49.9
    assert [d["tool"] for d in check_and_demote()] == ["update_price"]


def test_no_roas_without_spend(fake_kpis):
    promote("launch_campaign")
    fake_kpis["roas"], fake_kpis["spend"] = None, 0.0
    assert check_and_demote() == []


# ----------------------------------------------------------------------------------------------- behaviour
def test_idempotent_never_promotes_and_one_incident_per_tool():
    promote("refund_order", "record_expense")
    enter_safe_mode("t")
    assert len(check_and_demote()) == 2
    assert check_and_demote() == []  # second run: nothing left to demote
    assert sorted(demotions()) == ["record_expense", "refund_order"]
    assert incident_subjects() == ["record_expense", "refund_order"]
    inc = next(i for i in incidents.list_incidents() if i["monitor"] == "autonomy_demotion")
    assert inc["severity"] == "P2" and "safe mode is on" in inc["detail"]
    assert set(stages().values()) == {1}  # stage-1 tools untouched, nothing promoted


def test_demotion_is_by_system_with_reason():
    promote("refund_order")
    enter_safe_mode("t")
    check_and_demote()
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.decision == "demoted")).one()
    assert row.actor == "system" and "safe mode" in row.detail["reason"] and row.detail["to"] == 1


def test_all_triggers_are_evaluated_each_call():
    promote("refund_order", "launch_campaign")
    ids = make_orders(10)
    refund(ids[:5])
    deny("support", 20)
    keys = {t.key for t in autonomy_guard.evaluate_triggers()}
    assert {"refund_abuse", "policy_deny_spike"} <= keys
    assert {d["tool"] for d in check_and_demote()} == {"refund_order", "launch_campaign"}


def test_never_raises(monkeypatch):
    promote("refund_order")
    enter_safe_mode("t")
    monkeypatch.setattr(autonomy, "demote", lambda *a, **k: 1 / 0)
    assert check_and_demote() == []
    monkeypatch.setattr(autonomy, "promoted_tools", lambda: 1 / 0)
    assert check_and_demote() == []
    with session() as s:
        assert s.scalars(select(ErrorRow).where(ErrorRow.component == "autonomy_guard")).first() is not None


# ----------------------------------------------------------------------------------------------- monitors
def test_run_monitors_demotes_at_the_end():
    promote("refund_order")
    enter_safe_mode("t")
    r = monitors.run_monitors()
    assert [d["tool"] for d in r["demoted"]] == ["refund_order"] and autonomy.current_stage("refund_order") == 1


def test_failure_inside_check_and_demote_does_not_break_the_loop(monkeypatch):
    def boom(now=None):
        raise RuntimeError("guard down")

    monkeypatch.setattr(autonomy_guard, "check_and_demote", boom)
    r = monitors.run_monitors()
    assert r["demoted"] == [] and "open" in r and r["monitor_errors"] == []
    with session() as s:
        assert s.scalars(select(ErrorRow).where(ErrorRow.component == "monitors")).first() is not None


def test_refund_abuse_monitor_raises_and_resolves():
    ids = make_orders(10)
    refund(ids[:3])
    monitors.run_monitors()
    assert open_for("refund_abuse") == [("refunds", "P2")]
    with session() as s:
        s.execute(delete(Refund))
    monitors.run_monitors()
    assert open_for("refund_abuse") == []


def test_policy_deny_spike_monitor_raises_and_resolves():
    deny("marketing", 20)
    monitors.run_monitors()
    assert open_for("policy_deny_spike") == [("marketing", "P2")]
    monitors.run_monitors(now=db.now() + timedelta(hours=2))
    assert open_for("policy_deny_spike") == []


# ----------------------------------------------------------------------------------------------- invariant
def _force(stage):
    set_setting(autonomy.STAGE_KEY.format(tool="refund_order"), str(stage))


def test_invariant_flags_a_forged_setting_and_passes_after_a_real_promotion():
    assert no_unearned_promotion() == []
    _force(2)
    assert [v["invariant"] for v in no_unearned_promotion()] == ["no_unearned_promotion"]
    _force(1)
    autonomy.set_stage("refund_order", 2, "kaan", "ok", evidence=EVID)
    assert no_unearned_promotion() == []


def test_invariant_flags_a_stage_kept_after_demotion_and_non_human_promotions():
    autonomy.set_stage("refund_order", 2, "kaan", "ok", evidence=EVID)
    autonomy.demote("refund_order", "x")
    _force(2)  # re-forged after the demotion
    assert len(no_unearned_promotion()) == 1
    _force(1)
    audit("system", "autonomy_stage", "promoted", {"tool": "refund_order", "from": 1, "to": 2, "evidence": EVID})
    _force(2)
    assert len(no_unearned_promotion()) == 1  # not a human


# ----------------------------------------------------------------------------------------------- chaos
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_autonomy_demotion_scenario(seed):
    result = run_scenario("autonomy-demotion", seed=seed)
    assert result["violations"] == [], result["violations"]
    assert [d["tool"] for d in result["stats"]["demoted"]] == ["refund_order"]
