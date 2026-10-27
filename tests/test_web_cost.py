"""D4 + R3: Anthropic server-tool use is counted, priced and capped per run; spec shapes are configurable."""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from aecom.config import get_settings
from aecom.db import AgentRun, AuditLog, session
from aecom.graph import agents
from aecom.llm import Script, server_tool_specs, server_tool_usage


def reply(searches: int = 0, fetches: int = 0, *, tool_call: bool = True) -> AIMessage:
    """The shape langchain-anthropic produces: raw API usage (incl. server_tool_use) lands in response_metadata."""
    calls = [{"name": "list_products", "args": {}, "id": f"toolu_{searches}_{fetches}", "type": "tool_call"}] if tool_call else []
    return AIMessage(
        content="searching" if tool_call else "done", tool_calls=calls,
        usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
        response_metadata={"model_provider": "anthropic", "usage": {
            "input_tokens": 1000, "output_tokens": 100,
            "server_tool_use": {"web_search_requests": searches, "web_fetch_requests": fetches}}})


class StubModel:
    """Stands in for ChatAnthropic: replays replies and records which server tools each bind carried."""

    def __init__(self, replies: list[AIMessage]):
        self.replies = list(replies)
        self.binds: list[list[str]] = []

    def bind_tools(self, tools, **_kw):
        self.binds.append([t["name"] for t in tools if isinstance(t, dict)])
        return self

    async def ainvoke(self, _messages):
        return self.replies.pop(0)


@pytest.fixture
def live(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_mode", "anthropic")
    return s


def run_with(monkeypatch, stub: StubModel):
    monkeypatch.setattr(agents, "get_model", lambda *_a, **_k: stub)
    res = asyncio.run(agents.run_agent("researcher", "research ebook niches"))
    with session() as s:
        row = s.scalars(select(AgentRun).where(AgentRun.agent_id == "researcher")).one()
        return res, (row.web_searches, row.web_fetches, row.cost_eur)


def test_server_tool_usage_reads_response_metadata():
    assert server_tool_usage(reply(3, 2)) == (3, 2)
    assert server_tool_usage(AIMessage(content="x")) == (0, 0)


def test_counts_and_cost_added(monkeypatch, live):
    monkeypatch.setattr(live, "web_search_price_eur", 0.01)
    monkeypatch.setattr(live, "web_fetch_price_eur", 0.0)
    stub = StubModel([reply(2, 1), reply(3, 0), reply(tool_call=False)])
    res, (searches, fetches, cost) = run_with(monkeypatch, stub)
    assert res.status == "ok" and (res.web_searches, res.web_fetches) == (5, 1)
    assert (searches, fetches) == (5, 1)
    tokens = live.cost_eur(agents.load_spec("researcher").model_tier, 3000, 300)
    assert cost == pytest.approx(tokens + 5 * 0.01) == pytest.approx(res.cost_eur)


def test_web_fetch_priced_by_setting(monkeypatch, live):
    monkeypatch.setattr(live, "web_fetch_price_eur", 0.5)
    _, (_, fetches, cost) = run_with(monkeypatch, StubModel([reply(0, 2), reply(tool_call=False)]))
    tokens = live.cost_eur(agents.load_spec("researcher").model_tier, 2000, 200)
    assert fetches == 2 and cost == pytest.approx(tokens + 1.0)


def test_cap_reached_rebinds_without_web_search(monkeypatch, live):
    monkeypatch.setattr(live, "web_search_max_per_run", 4)
    stub = StubModel([reply(2), reply(2), reply(1), reply(tool_call=False)])
    res, (searches, _, _) = run_with(monkeypatch, stub)
    assert res.status == "ok" and searches == 5
    assert stub.binds[0] == ["web_search", "web_fetch"]
    assert stub.binds[1:] == [["web_fetch"]]  # rebound once, after the second call reached the cap
    with session() as s:
        caps = list(s.scalars(select(AuditLog).where(AuditLog.action == "web_search_cap")))
    assert len(caps) == 1 and caps[0].detail["web_searches"] == 4


def test_scripted_mode_counts_zero(monkeypatch):
    Script.load({"researcher": [{"text": "brief"}]})
    res = asyncio.run(agents.run_agent("researcher", "research"))
    with session() as s:
        row = s.scalars(select(AgentRun).where(AgentRun.agent_id == "researcher")).one()
    assert (res.web_searches, res.web_fetches, row.web_searches, row.web_fetches) == (0, 0, 0, 0)
    assert server_tool_specs(["web_search", "web_fetch"]) == []


def test_spec_shapes(monkeypatch, live):
    for k, v in {"web_search_max_uses": 7, "web_fetch_max_content_tokens": 1234,
                 "web_search_allowed_domains": [], "web_search_blocked_domains": [],
                 "web_fetch_allowed_domains": [], "web_fetch_blocked_domains": []}.items():
        monkeypatch.setattr(live, k, v)
    search, fetch = server_tool_specs(["web_search", "web_fetch"])
    assert search == {"type": live.web_search_tool_type, "name": "web_search", "max_uses": 7}
    assert fetch == {"type": live.web_fetch_tool_type, "name": "web_fetch", "max_uses": live.web_fetch_max_uses,
                     "max_content_tokens": 1234}
    monkeypatch.setattr(live, "web_search_blocked_domains", ["spam.example"])
    monkeypatch.setattr(live, "web_fetch_allowed_domains", ["docs.example"])
    monkeypatch.setattr(live, "web_fetch_blocked_domains", ["ignored.example"])  # API takes only one of the two
    search, fetch = server_tool_specs(["web_search", "web_fetch"])
    assert search["blocked_domains"] == ["spam.example"] and "allowed_domains" not in search
    assert fetch["allowed_domains"] == ["docs.example"] and "blocked_domains" not in fetch


def test_defaults():
    s = get_settings()
    assert s.web_search_tool_type == "web_search_20260209"
    assert (s.web_search_max_uses, s.web_search_max_per_run, s.web_fetch_max_content_tokens) == (5, 15, 8000)
    assert (s.web_search_price_eur, s.web_fetch_price_eur) == (0.01, 0.0)
