"""R19: hardening the Phase 5 autonomy stages (docs/wiki/Roadmap.md row R19, docs/wiki/Autonomy.md).

(a) ``trust.promote`` no longer checks eligibility and writes the stage as two independent steps: the write is a
    guarded compare-and-set (``autonomy.set_stage(..., expected_old_stage=...)``) against the exact stage the
    eligibility check saw, so a promotion or an automatic demotion that lands in between is detected instead of
    silently overwritten.
(b) ``autonomy_guard.check_and_demote`` now raises its P2 incident BEFORE writing the demotion, so a failure in
    between the two never loses the incident - the audit trail (the open incident) survives, and the demotion
    itself is retried, idempotently, on the next monitor tick.
(c) a stage-3 ("notify") autonomous action now also sends an immediate owner notification
    (``aecom.notify.route``, kind ``autonomy``) instead of only showing up in the daily flash and the log.

(d) is deliberately NOT covered here: see docs/wiki/Autonomy.md "Limits to know" and the R19 report for why the
    Judge's verdict is not available per autonomous action in the existing data (only per ``run_id``, shared by
    unrelated actions/tools), so no demotion trigger was added for it.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom import autonomy, autonomy_guard, incidents, trust
from aecom.audit import audit
from aecom.autonomy_guard import check_and_demote
from aecom.bootstrap import seed_demo
from aecom.company_settings import enter_safe_mode
from aecom.db import Approval, AuditLog, Customer, Order, Product, now, session

TOOL = "refund_order"
EVID = {"real_sales": 12, "approvals": 8, "approve_unchanged_rate": 0.95}


def _ready(tool: str = TOOL) -> None:
    """Enough real sales and clean, unanimous approvals for ``tool`` to be eligible for its next stage."""
    seed_demo()
    with session() as s:
        pid = s.scalars(select(Product.id)).first()
        for i in range(10):
            c = Customer(email=f"h19-{i}@example.com", country="DE")
            s.add(c)
            s.flush()
            s.add(Order(customer_id=c.id, product_id=pid, amount_eur=10.0, status="paid",
                        external_id=f"ext-h19-{i}"))
    with session() as s:
        for _ in range(20):
            s.add(Approval(agent_id="support", tool_name=tool, args={}, risk_tier="T2", reason="r",
                           status="approved", args_edited=False, decided_by="kaan",
                           created_at=now() - timedelta(minutes=10), decided_at=now()))


def incident_subjects() -> list[str]:
    return sorted(i["subject"] for i in incidents.list_incidents() if i["monitor"] == "autonomy_demotion")


def audit_rows(action: str = "autonomy_stage") -> list[AuditLog]:
    with session() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


# ================================================================================================= (a) promote()
def test_promote_rejects_a_stage_that_moved_between_check_and_write(monkeypatch):
    _ready()
    frozen = trust.eligibility(TOOL)
    assert frozen["eligible"] and (frozen["stage"], frozen["next_stage"]) == (1, 2)
    monkeypatch.setattr(trust, "eligibility", lambda tool, now=None: frozen)
    # a concurrent human promotion (or an automatic demotion) wins the race after the check above
    autonomy.set_stage(TOOL, 2, "racer", "won the race", evidence=EVID)
    with pytest.raises(ValueError, match="concurrently"):
        trust.promote(TOOL, "kaan", "go")
    assert autonomy.current_stage(TOOL) == 2  # the loser never got to write
    rows = audit_rows()
    assert len(rows) == 1 and rows[0].actor == "human:racer"  # only the winner's promotion is audited


def test_promote_still_works_normally_without_a_race():
    _ready()
    assert trust.promote(TOOL, "kaan", "earned it") == 2
    assert autonomy.current_stage(TOOL) == 2
    a = audit_rows()[-1]
    assert a.detail["evidence"]["checked_at"]


def test_set_stage_cas_matches_and_mismatches():
    assert autonomy.set_stage(TOOL, 2, "kaan", "ok", evidence=EVID, expected_old_stage=1) == 2
    with pytest.raises(autonomy.StageConflict):
        autonomy.set_stage(TOOL, 3, "kaan", "ok", evidence=EVID, expected_old_stage=1)  # stale: really 2 now
    assert autonomy.current_stage(TOOL) == 2  # untouched by the rejected attempt


def test_set_stage_cas_is_fine_with_a_matching_expectation():
    assert autonomy.set_stage(TOOL, 2, "kaan", "ok", evidence=EVID, expected_old_stage=1) == 2
    assert autonomy.set_stage(TOOL, 3, "kaan", "ok", evidence=EVID, expected_old_stage=2) == 3
    assert autonomy.current_stage(TOOL) == 3


def test_a_concurrent_call_that_already_made_the_same_change_is_a_no_op_not_a_mislabeled_demotion():
    autonomy.set_stage(TOOL, 2, "kaan", "ok", evidence=EVID)
    # a second call (e.g. a racing demote(), which never passes expected_old_stage) targeting the already-current
    # stage must be a quiet no-op, not write a bogus "demoted" row just because the (stale) caller thought it was
    # still raising from 1.
    assert autonomy.set_stage(TOOL, 2, "kaan", "ok", evidence=EVID) == 2
    rows = audit_rows()
    assert len(rows) == 1 and rows[0].decision == "promoted"  # no bogus second row for a same-stage no-op


# ================================================================================================= (b) demotion order
def test_incident_is_raised_before_demotion_so_a_failing_demote_never_loses_it(monkeypatch):
    autonomy.set_stage(TOOL, 2, "kaan", "seed", evidence=EVID)
    enter_safe_mode("t")
    real_demote = autonomy.demote
    monkeypatch.setattr(autonomy, "demote", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert check_and_demote() == []  # the demotion write failed
    assert incident_subjects() == [TOOL]  # ... but the P2 incident survived it
    assert autonomy.current_stage(TOOL) == 2  # the tool itself is not demoted yet

    monkeypatch.setattr(autonomy, "demote", real_demote)
    done = check_and_demote()  # next tick: the still-open incident is reused, the demotion now completes
    assert [d["tool"] for d in done] == [TOOL]
    assert incident_subjects() == [TOOL]  # still exactly one incident, not duplicated
    assert autonomy.current_stage(TOOL) == 1


def test_demotion_still_audits_as_before():
    autonomy.set_stage(TOOL, 2, "kaan", "seed", evidence=EVID)
    enter_safe_mode("t")
    check_and_demote()
    row = audit_rows()[-1]
    assert row.actor == "system" and row.decision == "demoted" and "safe mode" in row.detail["reason"]


# ================================================================================================= (c) stage-3 notify
def test_stage3_autonomous_action_sends_an_immediate_notification(monkeypatch):
    sent = []
    monkeypatch.setattr("aecom.notify.route", lambda event: sent.append(event) or {"dashboard": True})
    audit("support", TOOL, "allow", {"autonomous": True, "autonomy_stage": 3, "args": {"amount_eur": 300}})
    n = autonomy_guard.notify_stage3_actions(now())
    assert n == 1 and len(sent) == 1
    event = sent[0]
    assert event["kind"] == "autonomy" and event["urgency"] == "normal" and TOOL in event["text"]
    # already notified: a second pass sends nothing more (the bookmark advanced)
    assert autonomy_guard.notify_stage3_actions(now()) == 0 and len(sent) == 1


def test_stage1_and_stage2_autonomous_actions_do_not_notify(monkeypatch):
    sent = []
    monkeypatch.setattr("aecom.notify.route", lambda event: sent.append(event) or {"dashboard": True})
    audit("support", TOOL, "allow", {"autonomous": True, "autonomy_stage": 2})
    audit("support", TOOL, "allow", {"autonomous": False, "autonomy_stage": 3})  # not autonomous: ignored too
    assert autonomy_guard.notify_stage3_actions(now()) == 0 and sent == []


def test_check_and_demote_also_sends_stage3_notifications():
    sent = []

    def fake_route(event):
        sent.append(event)
        return {"dashboard": True}

    import aecom.notify as notify_module

    original = notify_module.route
    notify_module.route = fake_route
    try:
        audit("finance", "record_expense", "allow", {"autonomous": True, "autonomy_stage": 3})
        assert check_and_demote() == []
        assert len(sent) == 1 and "record_expense" in sent[0]["text"]
    finally:
        notify_module.route = original


def test_notify_failure_is_swallowed_and_never_breaks_check_and_demote(monkeypatch):
    monkeypatch.setattr("aecom.notify.route", lambda event: (_ for _ in ()).throw(RuntimeError("channel down")))
    audit("support", TOOL, "allow", {"autonomous": True, "autonomy_stage": 3})
    assert autonomy_guard.notify_stage3_actions(now()) == 0  # the send failed, so it does not count as sent
    assert check_and_demote() == []  # and the rest of the monitor tick still runs cleanly
