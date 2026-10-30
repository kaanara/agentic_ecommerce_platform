"""consult_agent (built only inside a chat run, aecom.graph.agents) goes through the same policy gate as every
other tool: policy.yaml's T1 rule restricted to the chat-eligible agents, kill switch, safe mode, agents-paused
and the audit/ToolCallLog trail all apply - while it is never listed in a persona's TOOLS.md, so it stays
invisible to automated (non-chat) runs."""

from __future__ import annotations

import pytest

from aecom import chat, conversations as conv, dashboard_auth
from aecom.company_settings import set_flag, set_setting
from aecom.db import AuditLog, ToolCallLog, session
from aecom.llm import Script
from aecom.policy.gate import evaluate
from aecom.registry.tools import allowed_tools
from aecom.tools.catalog import CATALOG

CHAT_AGENTS = ["ceo", "finance", "researcher", "sentinel", "foreman"]
NON_CHAT_AGENTS = ["store_ops", "support", "marketing", "supply", "judge"]


@pytest.fixture(autouse=True)
def _login_on():
    dashboard_auth.set_password("a-strong-password-123", "a-strong-password-123", "tester")
    yield


def _consult(agent_id: str) -> None:
    cid = conv.create(agent_id, "alice")
    Script.load({
        agent_id: [{"text": "asking finance", "tool_calls": [{"name": "consult_agent",
                                                              "args": {"agent": "researcher", "question": "ok?"}}]},
                  {"text": "done"}],
        "researcher": [{"text": "looks fine"}],
    })
    return chat.converse(cid, "consult researcher on this")


# --------------------------------------------------------------------------- policy classification


def test_consult_agent_is_t1_restricted_to_chat_agents():
    for agent in CHAT_AGENTS:
        v = evaluate(agent, "consult_agent", {"agent": "finance", "question": "x"})
        assert v.tier == "T1"
        assert v.decision == "allow"


def test_consult_agent_denied_for_non_chat_agents_by_the_policy_rule():
    for agent in NON_CHAT_AGENTS:
        v = evaluate(agent, "consult_agent", {"agent": "finance", "question": "x"})
        assert v.decision == "deny"
        assert "consult_agent" in v.reason


def test_consult_agent_is_never_a_catalog_tool_or_allowlisted():
    """Built only inside a chat run (graph/agents.py) - never a tool an agent can be granted via TOOLS.md, so it
    never reaches automated (non-chat) runs regardless of the policy rule above."""
    assert "consult_agent" not in CATALOG
    for agent in [*CHAT_AGENTS, *NON_CHAT_AGENTS]:
        assert "consult_agent" not in allowed_tools(agent)


# --------------------------------------------------------------------------- end to end through chat


def test_consult_agent_runs_through_the_gate_and_is_logged():
    turn = _consult("ceo")
    assert turn.status == "ok"
    step = next(s for s in turn.tool_steps if s["name"] == "consult_agent")
    assert step["decision"] == "ok"
    with session() as s:
        call = (s.query(ToolCallLog).filter_by(tool_name="consult_agent").order_by(ToolCallLog.id.desc()).first())
        assert call is not None
        assert call.agent_id == "ceo" and call.decision == "allow" and call.ok is True
        audits = list(s.query(AuditLog).filter_by(action="consult_agent"))
        assert audits and audits[-1].decision == "allow"


def test_consult_agent_denied_when_kill_switch_is_on():
    set_flag("kill_switch", True, by="tester")
    turn = _consult("ceo")
    step = next(s for s in turn.tool_steps if s["name"] == "consult_agent")
    assert step["decision"] == "denied"
    assert "Kill switch" in step["result"]


def test_consult_agent_denied_in_safe_mode():
    set_setting("safe_mode", "on")
    turn = _consult("ceo")
    step = next(s for s in turn.tool_steps if s["name"] == "consult_agent")
    assert step["decision"] == "denied"
    assert "Safe mode" in step["result"]


def test_consult_agent_denied_when_agents_paused():
    set_flag("agents_paused", True, by="tester")
    turn = _consult("ceo")
    step = next(s for s in turn.tool_steps if s["name"] == "consult_agent")
    assert step["decision"] == "denied"
    assert "paused" in step["result"]
