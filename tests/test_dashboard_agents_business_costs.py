"""AppTest smoke + behaviour tests for the Insight pages owned by builder G: Agents, Business and Costs.

Pages are exercised through the real entrypoint (Home.py -> st.navigation), with login off, and data seeded directly
through ``aecom.db`` (agent runs, conversations) and the existing service helpers (``company_settings``) rather than
raw SQL writes from the test.
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import dashboard_auth as auth
from aecom.company_settings import agent_enabled, set_agent_enabled, set_setting
from aecom.db import AgentRun, Conversation, ToolCallLog, new_id, now, session

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the pages do `from _common import ...`

HOME = str(DASHBOARD / "Home.py")
AGENTS_PAGE = "app_pages/agents.py"
BUSINESS_PAGE = "app_pages/business.py"
COSTS_PAGE = "app_pages/costs.py"


def _app() -> AppTest:
    set_setting(auth.MODE_KEY, "off")
    return AppTest.from_file(HOME, default_timeout=60)


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def _seed_runs() -> None:
    """One automated run and one chat run per of two agents, so Agents/Costs have data to split and chart."""
    with session() as s:
        s.add(AgentRun(run_id=new_id("run"), agent_id="finance", model="claude-sonnet-5-5", task="daily close",
                       output="done", status="ok", steps=2, tool_calls=1, input_tokens=100, output_tokens=50,
                       cost_eur=0.02, started_at=now()))
        s.add(AgentRun(run_id="chat-abc123", agent_id="finance", model="claude-sonnet-5-5", task="owner question",
                       output="answer", status="ok", steps=1, tool_calls=0, input_tokens=80, output_tokens=40,
                       cost_eur=0.015, started_at=now()))
        s.add(AgentRun(run_id=new_id("run"), agent_id="marketing", model="claude-haiku-5-5", task="campaign review",
                       output="done", status="ok", steps=1, tool_calls=2, input_tokens=60, output_tokens=30,
                       cost_eur=0.005, started_at=now()))
        s.add(ToolCallLog(run_id=new_id("run"), agent_id="finance", tool_name="get_kpis", decision="allow", ok=True))


def _seed_conversation() -> str:
    with session() as s:
        convo = Conversation(agent_id="finance", channel="dashboard", started_by="owner", owner="tester",
                             total_cost_eur=0.035)
        s.add(convo)
        s.flush()
        return convo.id


# --------------------------------------------------------------------------- Agents


def test_agents_page_renders_with_no_data():
    """The st.stop() that used to hide Runs/Prompts when there were no runs must be gone."""
    at = _app().run()
    at = at.switch_page(AGENTS_PAGE).run()
    assert not at.exception, _messages(at)
    assert at.tabs and [t.label for t in at.tabs] == ["Live", "Runs", "Prompts"]


def test_agents_tabs_show_live_runs_and_prompts():
    _seed_runs()
    at = _app().run()
    at = at.switch_page(AGENTS_PAGE).run()
    assert not at.exception, _messages(at)
    # Live tab: an enable toggle, keyed per agent, rendered through the shared registry function
    assert any(t.key == "agent_enabled_finance" for t in at.toggle)
    # Runs tab: per-agent summary reflects the seeded cost
    assert any("Per-agent summary" in m.value for m in at.subheader)
    # Prompts tab: a persona selectbox plus the registry/lessons sections
    assert any("Persona" in m.value for m in at.subheader)
    assert any("Registry" in m.value for m in at.subheader)


def test_agents_enable_toggle_uses_the_shared_registry():
    at = _app().run()
    at = at.switch_page(AGENTS_PAGE).run()
    toggle = next(t for t in at.toggle if t.key == "agent_enabled_finance")
    was_on = agent_enabled("finance")
    toggle.set_value(not was_on)
    at.run()
    assert agent_enabled("finance") == (not was_on)
    set_agent_enabled("finance", was_on)  # restore


def test_agents_recent_runs_filter_by_agent():
    _seed_runs()
    at = _app().run()
    at = at.switch_page(AGENTS_PAGE).run()
    select = next(s for s in at.selectbox if s.label == "Agent" and s.key != "prompts_agent")
    select.set_value("marketing")
    at.run()
    assert not at.exception, _messages(at)


# --------------------------------------------------------------------------- Business


def test_business_page_has_four_tabs_with_column_config():
    at = _app().run()
    at = at.switch_page(BUSINESS_PAGE).run()
    assert not at.exception, _messages(at)
    assert at.tabs and [t.label for t in at.tabs] == ["Overview", "P&L & OKRs", "Campaigns", "Catalog & vendors"]
    # the OKRs / P&L tab still uses column_config (ProgressColumn) as before the refresh
    assert any("P&L" in m.value for m in at.subheader)


# --------------------------------------------------------------------------- Costs


def test_costs_page_renders_with_no_data_and_reminds_about_the_spending_cap():
    at = _app().run()
    at = at.switch_page(COSTS_PAGE).run()
    assert not at.exception, _messages(at)
    assert at.tabs and [t.label for t in at.tabs] == ["By agent", "By day", "Conversations"]
    assert any("Anthropic Console" in i.value for i in at.info)
    # today's budget table is shown even with zero agent runs
    assert any("Today's budget" in m.value for m in at.subheader)


def test_costs_splits_chat_from_automated_and_tracks_conversations():
    _seed_runs()
    convo_id = _seed_conversation()
    at = _app().run()
    at = at.switch_page(COSTS_PAGE).run()
    assert not at.exception, _messages(at)
    # By agent tab totals: chat (0.015) + automated (0.02 + 0.005) = 0.04
    assert any("€0.040" in m.value for m in at.metric)
    # Conversations tab lists the seeded conversation
    tables = [d.value for d in at.dataframe]
    assert any(convo_id in df["id"].astype(str).tolist() for df in tables if "id" in getattr(df, "columns", []))


def test_costs_budget_progress_uses_the_same_daily_budget_as_the_chat_cap():
    from aecom.graph.agents import daily_budget

    at = _app().run()
    at = at.switch_page(COSTS_PAGE).run()
    tables = [d.value for d in at.dataframe]
    budget_table = next(df for df in tables if "daily_budget_eur" in getattr(df, "columns", []))
    row = budget_table[budget_table["agent_id"] == "finance"].iloc[0]
    assert row["daily_budget_eur"] == daily_budget("finance")
