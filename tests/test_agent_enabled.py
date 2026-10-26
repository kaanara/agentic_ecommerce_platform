"""Per-agent enabled flag (L3): disabled agents skip their LLM runs; reflexes keep working."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain, publish
from aecom.company_settings import agent_enabled, get_setting, set_agent_enabled
from aecom.db import AgentRun, AuditLog, EventRow, session
from aecom.graph import agents
from aecom.llm import Script
from aecom.scenarios import final, tc
from aecom.store.adapter import get_store


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)


def no_model(*_a, **_k):
    raise AssertionError("the model must not be called for a disabled agent")


def disabled_audits():
    with session() as s:
        return [a.detail for a in s.scalars(select(AuditLog).where(AuditLog.action == "agent_run", AuditLog.decision == "deny"))
                if a.detail.get("reason") == "agent disabled"]


def test_default_is_enabled_and_flag_round_trips():
    assert agent_enabled("marketing") and get_setting("agent_enabled:marketing") is None
    set_agent_enabled("marketing", False)
    assert not agent_enabled("marketing") and get_setting("agent_enabled:marketing") == "off"
    set_agent_enabled("marketing", True)
    assert agent_enabled("marketing")


def test_disabled_agent_does_not_call_the_model(monkeypatch):
    monkeypatch.setattr(agents, "get_model", no_model)
    set_agent_enabled("marketing", False)
    result = asyncio.run(agents.run_agent("marketing", "Write a tweet"))
    assert result.status == "disabled" and "disabled" in result.output
    assert [d["agent_id"] for d in disabled_audits()] == ["marketing"]
    with session() as s:
        assert s.scalars(select(AgentRun).where(AgentRun.agent_id == "marketing")).all() == []


def test_re_enabled_agent_runs_again():
    set_agent_enabled("marketing", False)
    assert asyncio.run(agents.run_agent("marketing", "Write a tweet")).status == "disabled"
    set_agent_enabled("marketing", True)
    Script.load({"marketing": [final("Tweet drafted.")]})
    result = asyncio.run(agents.run_agent("marketing", "Write a tweet"))
    assert result.status == "ok" and result.output == "Tweet drafted."
    with session() as s:
        assert [r.status for r in s.scalars(select(AgentRun).where(AgentRun.agent_id == "marketing"))] == ["ok"]


def test_paid_order_is_delivered_while_support_and_store_ops_are_disabled(no_backoff, monkeypatch):
    monkeypatch.setattr(agents, "get_model", no_model)
    seed_demo()
    for agent_id in ("support", "store_ops", "supply"):
        set_agent_enabled(agent_id, False)
    order = get_store().create_order("buyer@example.com", "LIC-FOCUSFLOW-PRO-V1", "DE")
    drain()
    assert get_store().get_order(order["id"])["status"] == "delivered"


def test_email_for_disabled_support_is_processed_not_retried(no_backoff, monkeypatch):
    monkeypatch.setattr(agents, "get_model", no_model)
    set_agent_enabled("support", False)
    eid = publish("email.received", {"from": "maria.k@example.com", "subject": "Hi", "body": "Hello", "message_id": "m9"})
    drain()
    with session() as s:
        row = s.get(EventRow, eid)
        assert row.processed and not row.dead and row.attempts <= 1
    assert events.status()["waiting_for_retry"] == 0
    reasons = disabled_audits()
    assert len(reasons) == 1 and reasons[0]["agent_id"] == "support" and "maria.k@example.com" in reasons[0]["task"]


def test_company_run_skips_disabled_department_and_mentions_it():
    from aecom.graph.supervisor import run_company

    seed_demo()
    set_agent_enabled("marketing", False)
    set_agent_enabled("judge", False)
    Script.load({
        "ceo": [final("Brief: price check and a launch tweet."), final("Done: price reviewed.")],
        "foreman": [tc("submit_plan", rationale="Price, then promote.", tasks=[
            {"id": "t1", "agent": "finance", "instruction": "Review the price of LIC-FOCUSFLOW-PRO-V1."},
            {"id": "t2", "agent": "marketing", "instruction": "Tweet about it.", "depends_on": ["t1"]}])],
        "finance": [final("Price is fine.")],
    })
    state = asyncio.run(run_company("Review pricing and promote"))
    assert state["skipped"] == ["t2: marketing disabled"]
    by_task = {r["task_id"]: r for r in state["results"]}
    assert by_task["t1"]["status"] == "ok" and by_task["t2"]["status"] == "disabled"
    assert state["verdict"]["unreviewed"] is True
    assert "Done: price reviewed." in state["summary"] and "t2: marketing disabled" in state["summary"]
    with session() as s:
        assert s.scalars(select(AgentRun).where(AgentRun.agent_id.in_(["marketing", "judge"]))).all() == []
