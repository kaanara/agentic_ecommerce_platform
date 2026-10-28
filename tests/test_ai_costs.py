"""R7: AI cost booking is atomic, crashed runs still pay for their tokens, the carry is stored cleanly."""

import asyncio
import re

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from aecom import ledger
from aecom.company_settings import get_setting
from aecom.config import get_settings
from aecom.db import AgentRun, LedgerEntry, session
from aecom.graph import agents


def reply() -> AIMessage:
    return AIMessage(
        content="searching", tool_calls=[{"name": "list_products", "args": {}, "id": "toolu_1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
        response_metadata={"model_provider": "anthropic", "usage": {
            "input_tokens": 1000, "output_tokens": 100, "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0}}})


class Crashing:
    """Answers once (tokens spent, one web search), then the API dies."""

    def __init__(self):
        self.calls = 0

    def bind_tools(self, *_a, **_k):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("api went away")
        return reply()


def runs() -> list[AgentRun]:
    with session() as s:
        return list(s.scalars(select(AgentRun).order_by(AgentRun.started_at)))


def ai_lines() -> int:
    with session() as s:
        return len(list(s.scalars(select(LedgerEntry).where(LedgerEntry.account == "ai_expense"))))


def flagged() -> int:
    return sum(1 for r in runs() if r.cost_booked)


@pytest.fixture
def cost(monkeypatch):
    monkeypatch.setattr(type(get_settings()), "cost_eur", lambda self, tier, tin, tout: 0.02)


def test_failure_after_ledger_post_leaves_no_partial_state(cost, monkeypatch):
    real_post = ledger.post

    def post_then_boom(*a, **k):
        real_post(*a, **k)
        raise RuntimeError("boom after post")

    monkeypatch.setattr(agents.ledger, "post", post_then_boom)
    res = asyncio.run(agents.run_agent("support", "hello"))
    assert res.status == "ok"  # booking failure does not fail the run
    assert runs()[0].status == "ok"
    assert ai_lines() == 0 and flagged() == 0
    assert get_setting("ai_cost_carry_eur", "0") in ("0", None)

    monkeypatch.setattr(agents.ledger, "post", real_post)
    assert agents.book_ai_costs() is not None
    assert ai_lines() == 1 and flagged() == 1
    assert agents.book_ai_costs() is None  # booked once only
    assert ai_lines() == 1


def test_flag_failure_rolls_back_ledger_line(cost, monkeypatch):
    monkeypatch.setattr(agents, "Setting", None)  # fails after post + flags, before commit
    assert asyncio.run(agents.run_agent("support", "hello")).status == "ok"
    with pytest.raises(Exception):
        agents.book_ai_costs()
    assert ai_lines() == 0 and flagged() == 0


def test_crashed_run_with_tokens_stores_and_books_cost(cost, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_mode", "anthropic")

    stub = Crashing()
    monkeypatch.setattr(agents, "get_model", lambda *_a, **_k: stub)
    res = asyncio.run(agents.run_agent("researcher", "research ebook niches"))
    assert res.status == "error"
    row = runs()[0]
    assert row.status == "error"
    assert (row.input_tokens, row.output_tokens, row.web_searches) == (1000, 100, 1)
    assert row.cost_eur == pytest.approx(0.02 + get_settings().web_cost_eur(1, 0))
    assert row.cost_booked is True
    assert ai_lines() == 1


def test_carry_is_quantized_to_eight_places(monkeypatch):
    monkeypatch.setattr(type(get_settings()), "cost_eur", lambda self, tier, tin, tout: 0.01121172)
    asyncio.run(agents.run_agent("support", "hello"))
    carry = get_setting("ai_cost_carry_eur", "0")
    assert re.fullmatch(r"-?\d+\.\d{8}", carry), carry
    assert carry == "0.00121172"


def test_tiny_runs_still_end_within_a_cent(monkeypatch):
    monkeypatch.setattr(type(get_settings()), "cost_eur", lambda self, tier, tin, tout: 0.0003)

    async def many() -> None:
        for _ in range(100):
            assert (await agents.run_agent("support", "hello")).status == "ok"

    asyncio.run(many())
    assert ledger.balances().get("ai_expense", 0.0) == pytest.approx(0.03, abs=0.01)
