"""Chat core: plan section 1 and 'Verification > Chat'.

Offline only: scripted LLM (conftest.py fixture), temporary DATABASE_URL, no network. Each test that needs
``chat.converse`` to actually run the agent turns the dashboard login on first (``blocked_reason`` requires it).
"""

from __future__ import annotations

import pytest

from aecom import chat, conversations as conv, dashboard_auth
from aecom.context import current_counterparty
from aecom.db import Approval, session
from aecom.graph import agents
from aecom.graph.agents import AgentResult, _run_core, run_agent
from aecom.llm import Script
from aecom.observability import mask


@pytest.fixture(autouse=True)
def _login_on():
    """Most chat behaviour requires the dashboard login to be on; the login-required test opts back out."""
    dashboard_auth.set_password("a-strong-password-123", "a-strong-password-123", "tester")
    yield


# --------------------------------------------------------------------------- run_agent / _run_core split


def test_run_agent_is_a_thin_wrapper_over_run_core(monkeypatch):
    seen = {}

    async def fake_core(agent_id, task, *, context="", run_id=None, history=None, chat=False, on_event=None, depth=0):
        seen.update(agent_id=agent_id, task=task, context=context, run_id=run_id, history=history, chat=chat,
                   on_event=on_event, depth=depth)
        return AgentResult(agent_id, "ok", status="ok")

    monkeypatch.setattr(agents, "_run_core", fake_core)
    import asyncio

    result = asyncio.run(run_agent("ceo", "do the thing", context="ctx", run_id="run-1"))
    assert result.output == "ok"
    assert seen == {"agent_id": "ceo", "task": "do the thing", "context": "ctx", "run_id": "run-1", "history": None,
                    "chat": False, "on_event": None, "depth": 0}


def test_run_core_scripted_output_unchanged_for_automated_runs():
    """Scenarios/chaos stay byte-identical: chat=False keeps the original single-shot message shape."""
    import asyncio

    Script.load({"ceo": [{"text": "the plain automated answer"}]})
    result = asyncio.run(run_agent("ceo", "status check"))
    assert result.status == "ok"
    assert result.output == "the plain automated answer"
    assert result.messages and result.messages[-1]["content"] == "the plain automated answer"
    assert result.messages[-1]["tool_steps"] == []


def test_run_core_messages_field_tracks_tool_steps():
    import asyncio

    Script.load({"finance": [
        {"text": "checking", "tool_calls": [{"name": "get_pnl", "args": {"period": "today"}}]},
        {"text": "done"},
    ]})
    result = asyncio.run(_run_core("finance", "what is our P&L?"))
    assert result.status == "ok"
    assert len(result.messages) == 2
    first_steps = result.messages[0]["tool_steps"]
    assert first_steps and first_steps[0]["name"] == "get_pnl"
    assert first_steps[0]["decision"] == "ok"


# --------------------------------------------------------------------------- chat mode behaviour


def test_chat_terminal_tools_are_removed():
    """submit_report (terminal, finance) is dropped from the tool list in chat mode: the model's call errors out."""
    cid = conv.create("finance", "alice")
    Script.load({"finance": [
        {"text": "let me submit", "tool_calls": [{"name": "submit_report", "args": {"kind": "daily_flash",
                                                                                     "period": "today", "text": "x"}}]},
        {"text": "noted, here is the summary instead"},
    ]})
    turn = chat.converse(cid, "give me a report")
    assert turn.status == "ok"
    assert turn.tool_steps
    step = turn.tool_steps[0]
    assert step["name"] == "submit_report"
    assert step["decision"] == "error"  # not available: filtered out because it is terminal
    assert "not available" in step["result"]


def test_chat_escalation_creates_approval():
    cid = conv.create("finance", "bob")
    Script.load({"finance": [
        {"text": "refunding", "tool_calls": [{"name": "refund_order",
                                              "args": {"order_id": "o-1", "amount_eur": 80.0, "reason": "goodwill"}}]},
        {"text": "escalated, awaiting approval"},
    ]})
    turn = chat.converse(cid, "refund order o-1 for 80 euros")
    assert turn.status == "ok"
    assert turn.escalations
    with session() as s:
        approval = s.get(Approval, turn.escalations[0])
        assert approval is not None
        assert approval.agent_id == "finance"
        assert approval.tool_name == "refund_order"
    stored = conv.get(cid)
    assert stored["messages"][-1]["escalations"] == turn.escalations


def test_chat_secrets_are_masked():
    cid = conv.create("ceo", "alice")
    secret = "sk-ant-api03-" + "x" * 40
    Script.load({"ceo": [{"text": f"your key {secret} is noted"}]})
    turn = chat.converse(cid, f"remember this key: {secret}")
    assert turn.status == "ok"
    assert secret not in turn.text
    stored = conv.get(cid)
    assert secret not in stored["messages"][0]["content"]
    assert secret not in stored["messages"][-1]["content"]
    assert mask(secret) in stored["messages"][0]["content"]


def test_chat_budget_share_is_enforced(monkeypatch):
    cid = conv.create("ceo", "alice")
    budget = agents.daily_budget("ceo")
    monkeypatch.setattr(chat, "spent_today", lambda agent_id: budget)  # already at/over the full budget
    Script.load({"ceo": [{"text": "should not be reached"}]})
    turn = chat.converse(cid, "expensive question")
    assert turn.status == "budget_exceeded"
    assert Script.queues["ceo"], "the model must not have been called"


def test_chat_counterparty_stays_unset():
    cid = conv.create("finance", "alice")
    Script.load({"finance": [{"text": "revenue is fine"}]})
    turn = chat.converse(cid, "what is our quarterly revenue?")
    assert turn.status == "ok"
    assert current_counterparty.get() is None


def test_chat_summaries_are_made():
    cid = conv.create("researcher", "alice")
    for i in range(50):
        conv.append_message(cid, "user", f"research question {i}")
        conv.append_message(cid, "assistant", f"research finding {i}", agent_id="researcher", cost_eur=0.001)
    Script.load({"researcher": [{"text": "here is the synthesis"}]})
    turn = chat.converse(cid, "synthesize all findings")
    assert turn.status == "ok"
    stored = conv.get(cid)
    assert stored["summary"]
    assert "research" in stored["summary"].lower() or "finding" in stored["summary"].lower()


def test_chat_consult_stops_at_depth_one():
    cid = conv.create("ceo", "alice")
    Script.load({
        "ceo": [{"text": "let me ask finance", "tool_calls": [{"name": "consult_agent",
                                                               "args": {"agent": "finance", "question": "ok?"}}]},
               {"text": "finance agrees"}],
        "finance": [{"text": "trying to ask researcher too",
                    "tool_calls": [{"name": "consult_agent", "args": {"agent": "researcher", "question": "x"}}]},
                   {"text": "my own answer, no further consulting"}],
    })
    turn = chat.converse(cid, "consult finance on this")
    assert turn.status == "ok"
    consult_step = next(s for s in turn.tool_steps if s["name"] == "consult_agent")
    assert consult_step["decision"] == "ok"
    import json

    nested = json.loads(consult_step["result"] if len(consult_step["result"]) < 500 else consult_step["result"])
    # the nested agent's own attempt to consult again must have failed (tool not offered at depth 1)
    assert any("not available" in str(m.get("tool_steps")) for m in nested.get("messages", []) if m.get("tool_steps")) \
        or "ERROR" in str(nested)


def test_chat_consult_limited_to_two_per_turn():
    cid = conv.create("ceo", "alice")
    Script.load({"ceo": [
        {"text": "1", "tool_calls": [{"name": "consult_agent", "args": {"agent": "finance", "question": "a"}}]},
        {"text": "2", "tool_calls": [{"name": "consult_agent", "args": {"agent": "researcher", "question": "b"}}]},
        {"text": "3", "tool_calls": [{"name": "consult_agent", "args": {"agent": "sentinel", "question": "c"}}]},
        {"text": "done"},
    ], "finance": [{"text": "ok"}], "researcher": [{"text": "ok"}]})
    turn = chat.converse(cid, "consult several agents")
    assert turn.status == "ok"
    consult_steps = [s for s in turn.tool_steps if s["name"] == "consult_agent"]
    assert len(consult_steps) == 3
    assert consult_steps[0]["decision"] == "ok"
    assert consult_steps[1]["decision"] == "ok"
    assert "limit" in consult_steps[2]["result"]  # third consult in the same turn is refused


def test_chat_attachments_are_fenced(tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "ATTACHMENT_DIR", tmp_path / "chat_files")
    cid = conv.create("finance", "alice")
    attachments = [
        {"type": "text", "name": "notes.txt", "content": "Sensitive pricing data"},
        {"type": "csv", "name": "prices.csv", "content": "SKU,Price\nPROD-1,99.99"},
    ]
    Script.load({"finance": [{"text": "reviewed the attachments"}]})
    turn = chat.converse(cid, "analyze this pricing data", attachments=attachments)
    assert turn.status == "ok"
    stored = conv.get(cid)
    assert stored["messages"][0]["attachments"] == attachments
    saved = list((tmp_path / "chat_files").glob("*"))
    assert len(saved) == 2


def test_chat_attachment_over_size_limit_is_skipped(monkeypatch):
    monkeypatch.setattr(chat.get_settings(), "chat_max_attachment_mb", 1)
    block = chat._attachment_block([{"type": "text", "name": "big.txt", "content": "x" * (2 * 1024 * 1024)}])
    assert "skipped" in block
    assert "exceeds" in block


def test_chat_login_required_or_paired_channel(monkeypatch):
    cid = conv.create("ceo", "alice")
    monkeypatch.setattr(dashboard_auth, "disabled", lambda: True)
    reason = chat.blocked_reason(paired_channel=False)
    assert reason is not None and "login" in reason.lower()

    turn = chat.converse(cid, "hello", paired_channel=False)
    assert turn.status == "blocked"

    Script.load({"ceo": [{"text": "hi there"}]})
    turn_paired = chat.converse(cid, "hello", paired_channel=True)
    assert turn_paired.status == "ok"


def test_chat_persists_user_message_first():
    cid = conv.create("ceo", "alice")
    Script.load({"ceo": [{"text": "noted"}]})
    chat.converse(cid, "remember this")
    stored = conv.get(cid)
    assert stored["messages"][0]["role"] == "user"
    assert stored["messages"][0]["content"] == mask("remember this")


def test_chat_turn_has_cost_and_agent_run_id():
    cid = conv.create("ceo", "alice")
    Script.load({"ceo": [{"text": "sure"}]})
    turn = chat.converse(cid, "tell me something")
    assert turn.cost_eur >= 0.0
    assert turn.agent_run_id and turn.agent_run_id.startswith("chat-")


def test_chat_on_event_streaming():
    cid = conv.create("ceo", "alice")
    events: list[dict] = []
    Script.load({"ceo": [{"text": "streamed reply"}]})
    turn = chat.converse(cid, "stream this", on_event=events.append)
    assert turn.status == "ok"
    assert {"type": "text_delta", "agent_id": "ceo", "text": "streamed reply"} in events


def test_chat_history_is_carried_over():
    cid = conv.create("ceo", "alice")
    for i in range(15):
        conv.append_message(cid, "user", f"question {i}")
        conv.append_message(cid, "assistant", f"answer {i}", agent_id="ceo", cost_eur=0.01)
    conv.set_summary(cid, "Previous conversation about Q1 planning")
    record = conv.get(cid)
    history = chat._build_history(record)
    # last chat_history_turns (12) turns = 24 messages, plus one summary message in front
    assert len(history) == 24 + 1
    assert history[0].content.startswith("(Summary of earlier conversation)")
    assert history[1].content == "question 3"  # oldest of the kept 12 turns


def test_chat_history_trims_old_tool_results():
    cid = conv.create("finance", "alice")
    conv.append_message(cid, "user", "check revenue")
    conv.append_message(cid, "assistant", "done", agent_id="finance",
                        tool_steps=[{"name": "get_pnl", "args": {}, "decision": "ok", "result": "y" * 2000}])
    record = conv.get(cid)
    history = chat._build_history(record)
    tool_line = next(m.content for m in history if "earlier tool calls" in m.content)
    assert len(tool_line) < 600
