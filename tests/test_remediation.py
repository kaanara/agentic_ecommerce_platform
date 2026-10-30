"""K5/K9: the Sentinel's remediation tools - every one through the policy gate, data fixes only as new entries."""

import asyncio
import inspect
from datetime import timedelta

import pytest
from aecom import approvals
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain, publish
from aecom.company_settings import agents_paused_on, kill_switch_on, safe_mode_on, set_flag
from aecom.db import Approval, AuditLog, EventRow, LedgerEntry, Order, OutboxEmail, session
from aecom.persona.loader import load_spec
from aecom.policy.gate import evaluate, gate_builtin
from aecom.registry.tools import allowed_tools, seed_registry
from aecom.store.adapter import get_store
from aecom.tools.catalog import CATALOG
from sqlalchemy import select

SKU = "LIC-FOCUSFLOW-PRO-V1"
REMEDIATION = {"replay_dead_letter": "T2", "rerun_reconciler": "T1", "resend_delivery_email": "T2",
               "pause_agents": "T1", "propose_correcting_entry": "T3"}
LINES = [["cash", 12.34, 0], ["revenue", 0, 12.34]]


@pytest.fixture(autouse=True)
def demo(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)
    seed_demo()
    drain()


def call(tool: str, agent: str = "sentinel", **args) -> str:
    return asyncio.run(gate_builtin(tool, agent).ainvoke(args))


def kill_dead(topic: str, order_id: str | None = None) -> int:
    """A dead-lettered event: for an order, its order.paid (never delivered); otherwise a fresh one."""
    if order_id:
        with session() as s:
            event_id = next(e.id for e in s.scalars(select(EventRow).where(EventRow.topic == topic))
                            if e.payload.get("order_id") == order_id)
    else:
        event_id = publish(topic, {"x": 1})
    with session() as s:
        row = s.get(EventRow, event_id)
        row.processed, row.dead, row.attempts, row.last_error = False, True, 5, "boom"
    return event_id


def emails_to(to: str) -> list:
    with session() as s:
        return list(s.scalars(select(OutboxEmail).where(OutboxEmail.direction == "out", OutboxEmail.to == to)))


def ledger_rows() -> list[tuple]:
    with session() as s:
        return [(e.id, e.txn_id, e.account, e.debit, e.credit, e.memo, e.ref)
                for e in s.scalars(select(LedgerEntry).order_by(LedgerEntry.id))]


def pending(tool: str) -> list[Approval]:
    with session() as s:
        return list(s.scalars(select(Approval).where(Approval.tool_name == tool, Approval.status == "pending")))


# ----------------------------------------------------------------------------- policy
def test_each_remediation_tool_is_gated_reserved_for_the_sentinel():
    for name, tier in REMEDIATION.items():
        assert CATALOG[name].side_effect and not CATALOG[name].terminal
        v = evaluate("sentinel", name, {})
        assert v.tier == tier
        assert v.decision == ("escalate" if tier == "T3" else "allow")
        for other in ("store_ops", "finance", "support", "ceo", "mcp_client"):
            assert evaluate(other, name, {}).decision == "deny"
    assert call("pause_agents", "support", reason="x").startswith("DENIED")
    assert not agents_paused_on()


def test_kill_switch_and_safe_mode_stop_remediation():
    set_flag("kill_switch", True, by="t")
    assert call("rerun_reconciler").startswith("DENIED")
    set_flag("kill_switch", False, by="t")
    from aecom.company_settings import set_setting

    set_setting("safe_mode", "on")
    assert call("pause_agents", reason="x").startswith("DENIED")


def test_sentinel_has_no_tool_to_undo_containment_or_edit_history():
    seed_registry()
    tools = set(allowed_tools("sentinel")) | set(load_spec("sentinel").tools)
    side_effecting = {t for t in tools if t in CATALOG and CATALOG[t].side_effect}
    assert side_effecting - {"message_owner"} == set(REMEDIATION)  # message_owner only notifies the owner
    assert list(inspect.signature(CATALOG["pause_agents"].fn).parameters) == ["reason"]  # can only switch it on
    for name in tools & set(CATALOG):  # nothing that unpauses, leaves safe mode, flips the kill switch, acknowledges
        src = inspect.getsource(CATALOG[name].fn)  # or edits ledger rows
        assert not any(w in name for w in ("unpause", "resume", "safe_mode", "kill", "acknowledge", "resolve"))
        assert not any(w in src for w in ("kill_switch", "leave_safe_mode", "set_flag(\"agents_paused\", False",
                                          "incidents.acknowledge", "incidents.resolve", "s.delete(", ".delete()",
                                          "update(LedgerEntry", "delete(LedgerEntry"))
    set_flag("agents_paused", True, by="t")
    assert call("pause_agents", reason="again").startswith("DENIED")  # and while paused it is stopped itself
    assert agents_paused_on() and not kill_switch_on() and not safe_mode_on()


# ----------------------------------------------------------------------------- replay_dead_letter
def test_replay_of_an_order_paid_dead_letter_delivers_the_order():
    order = get_store().create_order("dead@example.com", SKU)
    event_id = kill_dead("order.paid", order["id"])
    drain()
    assert get_store().get_order(order["id"])["status"] == "paid"
    out = call("replay_dead_letter", event_id=event_id)
    assert '"state": "processed"' in out
    assert get_store().get_order(order["id"])["status"] == "delivered"
    assert len(emails_to("dead@example.com")) == 1
    with session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.actor == "sentinel", AuditLog.action == "replay_dead_letter",
                                                AuditLog.decision == "allow")).one().detail["tier"] == "T2"
    assert call("replay_dead_letter", event_id=event_id).startswith("ERROR")  # no longer in the dead letter


def test_replay_outside_the_routine_topics_escalates_and_runs_once_approved():
    event_id = kill_dead("campaign.unknown_event")
    out = call("replay_dead_letter", event_id=event_id)
    assert out.startswith("ESCALATED to human approval (T2")
    with session() as s:
        assert s.get(EventRow, event_id).dead is True  # nothing replayed
    (a,) = pending("replay_dead_letter")
    assert a.risk_tier == "T2" and a.agent_id == "sentinel" and a.args == {"event_id": event_id}
    call("replay_dead_letter", event_id=event_id)  # asking again does not park a second approval
    assert len(pending("replay_dead_letter")) == 1
    assert approvals.approve(a.id, by="kaan")["status"] == "executed"
    with session() as s:
        e = s.get(EventRow, event_id)
        assert e.dead is False and e.processed is True


# ----------------------------------------------------------------------------- rerun_reconciler
def test_rerun_reconciler_requeues_and_delivers_a_lost_order():
    from aecom import db

    order = get_store().create_order("lost@example.com", SKU)
    kill_dead("order.paid", order["id"])
    with session() as s:
        s.get(Order, order["id"]).created_at = db.now() - timedelta(minutes=30)
    out = call("rerun_reconciler")
    assert '"requeued": 1' in out
    assert get_store().get_order(order["id"])["status"] == "delivered"


# ----------------------------------------------------------------------------- resend_delivery_email
def test_resend_delivery_email_only_for_delivered_unemailed_orders():
    order = get_store().create_order("mail@example.com", SKU)
    drain()
    assert len(emails_to("mail@example.com")) == 1
    out = call("resend_delivery_email", order_id=order["id"])  # already emailed: refused, no second mail
    assert out.startswith("ERROR") and "already" in out
    assert len(emails_to("mail@example.com")) == 1

    with session() as s:
        s.get(Order, order["id"]).email_sent = False  # the send failed earlier
    out = call("resend_delivery_email", order_id=order["id"])
    assert '"email_sent": true' in out
    assert len(emails_to("mail@example.com")) == 2
    with session() as s:
        assert s.get(Order, order["id"]).email_sent is True

    paid = get_store().create_order("paid@example.com", SKU)  # not delivered yet: the reconciler's job
    assert call("resend_delivery_email", order_id=paid["id"]).startswith("ERROR")


# ----------------------------------------------------------------------------- pause_agents
def test_pause_agents_sets_the_flag_and_audits_the_reason():
    out = call("pause_agents", reason="inc_1: duplicate keys")
    assert '"agents_paused": true' in out and agents_paused_on()
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.actor == "sentinel", AuditLog.action == "pause_agents",
                                               AuditLog.decision == "on")).one()
        assert row.detail["reason"] == "inc_1: duplicate keys"
        assert s.scalars(select(AuditLog).where(AuditLog.action == "agents_paused")).one().decision == "on"
    order = get_store().create_order("still@example.com", SKU)  # deliveries keep running
    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"


# ----------------------------------------------------------------------------- propose_correcting_entry (K9)
def test_correcting_entry_is_a_t3_approval_then_appended_never_an_edit():
    before = ledger_rows()
    out = call("propose_correcting_entry", lines=LINES, memo="missing sale line", ref="ord_x")
    assert out.startswith("ESCALATED to human approval (T3")
    assert ledger_rows() == before  # nothing posted yet
    (a,) = pending("propose_correcting_entry")
    assert a.agent_id == "sentinel" and a.risk_tier == "T3"

    assert approvals.approve(a.id, by="kaan")["status"] == "executed"
    after = ledger_rows()
    assert after[:len(before)] == before  # history unchanged
    new = after[len(before):]
    assert {r[1] for r in new} and len({r[1] for r in new}) == 1  # one new transaction
    assert sorted((r[2], r[3], r[4]) for r in new) == [("cash", 12.34, 0.0), ("revenue", 0.0, 12.34)]
    assert all(r[5] == "correction: missing sale line" and r[6] == "ord_x" for r in new)
    assert sum(r[3] for r in new) == pytest.approx(sum(r[4] for r in new))


@pytest.mark.parametrize("lines", [
    [["cash", 10, 0], ["revenue", 0, 9.99]],  # off by a cent
    [["cash", 10, 0]],  # one line
    [["cash", 10, 10], ["revenue", 0, 0]],  # both sides on one line
    [["cassh", 10, 0], ["revenue", 0, 10]],  # unknown account
])
def test_invalid_correcting_entries_are_rejected_and_post_nothing(lines):
    before = ledger_rows()
    call("propose_correcting_entry", lines=lines, memo="fix")
    (a,) = pending("propose_correcting_entry")
    assert approvals.approve(a.id, by="kaan")["status"] == "failed"
    assert ledger_rows() == before


def test_correcting_entry_is_never_posted_without_an_approval():
    before = ledger_rows()
    with pytest.raises(PermissionError):
        CATALOG["propose_correcting_entry"].fn(lines=LINES, memo="sneaky")
    assert ledger_rows() == before
