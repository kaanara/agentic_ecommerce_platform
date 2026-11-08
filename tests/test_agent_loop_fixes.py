"""Agent loop: bad tool args are retryable, pause_turn resumes; get_company_setup; ntfy bearer auth."""

import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from aecom import notify
from aecom.company_settings import set_setting
from aecom.graph import agents
from aecom.secrets import secrets
from aecom.tools.catalog import CATALOG


class Scripted:
    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def bind_tools(self, *_a, **_k):
        return self

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def call(name, args, id_="t1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_, "type": "tool_call"}])


def test_bad_tool_args_return_error_and_run_continues(monkeypatch):
    model = Scripted([call("submit_plan", {"rationale": "x"}), AIMessage(content="giving up")])
    monkeypatch.setattr(agents, "get_model", lambda *_a, **_k: model)
    res = asyncio.run(agents.run_agent("foreman", "plan"))
    assert res.status == "ok"
    assert res.messages[0]["tool_steps"][0]["decision"] == "error"
    tool_msgs = [m for m in model.seen[1] if isinstance(m, ToolMessage)]
    assert tool_msgs and tool_msgs[0].content.startswith("ERROR: invalid arguments for submit_plan")


def test_pause_turn_calls_model_again_without_tool_message(monkeypatch):
    paused = AIMessage(content="working", response_metadata={"stop_reason": "pause_turn"})
    model = Scripted([paused, AIMessage(content="done")])
    monkeypatch.setattr(agents, "get_model", lambda *_a, **_k: model)
    res = asyncio.run(agents.run_agent("support", "hi"))
    assert res.status == "ok" and res.output == "done"
    assert len(model.seen) == 2
    assert not any(isinstance(m, ToolMessage) for m in model.seen[1])
    assert model.seen[1][-1] is paused


def test_get_company_setup(monkeypatch, fresh_company):
    monkeypatch.setattr(fresh_company, "store_adapter", "mock")  # a local .env may select a real store
    from aecom.store.adapter import get_store
    get_store().create_product("SKU-1", "Thing", 10.0, "download", unit_cost_eur=1.0)
    set_setting("marketing_budget_month_eur", "150")
    set_setting("vendor_budget_month_eur", "40.5")
    out = CATALOG["get_company_setup"].fn()
    assert out["store_adapter"] == "mock"
    assert sum(out["products_by_status"].values()) == 1
    assert out["marketing_budget_month_eur"] == 150.0 and out["vendor_budget_month_eur"] == 40.5
    assert out["store_status"] in ("ok", "todo", "warn")


def test_ntfy_sends_bearer_token(monkeypatch):
    monkeypatch.setattr(secrets, "get", lambda n: {"NTFY_URL": "https://ntfy.sh/t", "NTFY_TOKEN": "tk_123"}.get(n))
    sent = {}

    class R:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: sent.update(kw) or R())
    assert notify._send_ntfy("hello", "high") is True
    assert sent["headers"]["Authorization"] == "Bearer tk_123"


def test_whop_tool_schema_keeps_args_name():
    from aecom.registry.whop_tool import build_whop_tool
    t = build_whop_tool("ceo")
    assert set(t.args) == {"operation", "args"}  # inferred schemas renamed `args` to `v__args`
    out = asyncio.run(t.ainvoke({"operation": "no.such.op", "args": {"id": "x"}}))
    assert out.startswith("ERROR: unknown operation")


def test_server_tool_callers_survive_resend():
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage
    from aecom.llm import restore_server_tool_callers
    caller = {"type": "code_execution_20260120", "tool_id": "srv_ce"}
    ai = AIMessage(content=[
        {"type": "server_tool_use", "id": "srv_ce", "name": "code_execution", "input": {}},
        {"type": "server_tool_use", "id": "srv_ws", "name": "web_search", "input": {"query": "q"}, "caller": caller},
        {"type": "web_search_tool_result", "tool_use_id": "srv_ws", "content": [], "caller": caller},
        {"type": "code_execution_tool_result", "tool_use_id": "srv_ce", "content": {}},
        {"type": "text", "text": "ok"}])
    msgs = [HumanMessage("hi"), ai, HumanMessage("next")]
    payload = ChatAnthropic(model="claude-sonnet-5-5", api_key="x")._get_request_payload(msgs)
    blocks = restore_server_tool_callers(msgs, payload)["messages"][1]["content"]
    assert [b.get("caller") for b in blocks] == [None, caller, caller, None, None]


def _latest_run(agent_id):
    from sqlalchemy import select

    from aecom.db import AgentRun, session
    with session() as s:
        row = s.scalars(select(AgentRun).where(AgentRun.agent_id == agent_id).order_by(AgentRun.started_at.desc())).first()
        return row.status, row.output


def test_interrupt_closes_the_run_row_and_propagates(monkeypatch):
    import asyncio as aio

    import pytest

    class Interrupted(Scripted):
        async def ainvoke(self, messages):
            raise aio.CancelledError()

    monkeypatch.setattr(agents, "get_model", lambda *_a, **_k: Interrupted([]))
    with pytest.raises(aio.CancelledError):
        asyncio.run(agents.run_agent("support", "hi"))
    status, output = _latest_run("support")
    assert status == "error" and "interrupted" in output  # not left 'running' for the doctor to find


def test_reap_stuck_runs_closes_only_old_running_rows():
    from datetime import timedelta

    from aecom.db import AgentRun, now, session
    with session() as s:
        s.add_all([AgentRun(id="run_old", run_id="r1", agent_id="researcher", model="m", task="t",
                            started_at=now() - timedelta(minutes=40)),
                   AgentRun(id="run_new", run_id="r2", agent_id="researcher", model="m", task="t"),
                   AgentRun(id="run_done", run_id="r3", agent_id="researcher", model="m", task="t", status="ok",
                            started_at=now() - timedelta(minutes=90))])
    assert agents.reap_stuck_runs() == ["run_old"]
    with session() as s:
        assert {r.id: r.status for r in s.query(AgentRun)} == {"run_old": "error", "run_new": "running",
                                                               "run_done": "ok"}
        assert s.get(AgentRun, "run_old").ended_at is not None
    assert agents.reap_stuck_runs() == []  # a second pass has nothing to do


class Recording(Scripted):
    def __init__(self, replies):
        super().__init__(replies)
        self.bound = []

    def bind_tools(self, tools, *_a, **_k):
        self.bound.append([getattr(t, "name", None) for t in tools])
        return self


def test_step_limited_run_is_told_to_finish_and_gets_only_the_terminal_tool(monkeypatch):
    from aecom.config import get_settings
    monkeypatch.setattr(get_settings(), "agent_max_steps", 3)
    model = Recording([call("list_products", {}, "a"), call("list_products", {}, "b"),
                       call("submit_brief", {"summary": "partial, gaps marked"}, "c")])
    monkeypatch.setattr(agents, "get_model", lambda *_a, **_k: model)
    res = asyncio.run(agents.run_agent("researcher", "research one product"))
    assert res.status == "ok" and res.structured["summary"] == "partial, gaps marked"
    nudge = model.seen[1][-1]  # before step 2, after the step-1 tool result
    assert nudge.type == "human" and "Call submit_brief now" in nudge.content
    assert not any("Call submit_brief now" in str(m.content) for m in model.seen[0])  # not before it is due
    assert model.bound[-1] == ["submit_brief"]  # the last step binds the terminal tool only
