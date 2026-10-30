import pandas as pd
import streamlit as st

from _common import (
    AGENT_ORDER,
    colors,
    empty_state,
    kpi_row,
    money,
    page_header,
    require_login,
    sql,
)
from aecom.config import get_settings
from aecom.graph.agents import daily_budget, spent_today

require_login()
page_header("Costs", "payments", "LLM spend by agent and by day, chat versus automated work, cost per conversation, "
           "and progress against the daily budgets checked before every agent run.")

settings = get_settings()
st.info(f"The budgets below are enforced per agent per day; once reached, that agent's LLM runs are deferred for "
       f"the rest of the day. Chat may use at most {settings.chat_daily_cap_share:.0%} of an agent's daily budget. "
       "This is not a hard ceiling on spend: also set a monthly spend limit on the Anthropic Console "
       "(console.anthropic.com) as a safety net outside the app.", icon=":material/warning:")

tab_agents, tab_days, tab_conversations = st.tabs(["By agent", "By day", "Conversations"])

totals = sql("""SELECT agent_id,
                       ROUND(SUM(CASE WHEN run_id LIKE 'chat-%' THEN cost_eur ELSE 0 END), 4) AS chat_eur,
                       ROUND(SUM(CASE WHEN run_id NOT LIKE 'chat-%' THEN cost_eur ELSE 0 END), 4) AS automated_eur,
                       ROUND(SUM(cost_eur), 4) AS total_eur
                FROM ops_agent_runs GROUP BY agent_id""")

with tab_agents:
    if totals.empty:
        empty_state("payments", "No agent runs yet", "Try `aecom scenario all --offline` or start a chat.")
    else:
        totals["order"] = totals["agent_id"].map({a: i for i, a in enumerate(AGENT_ORDER)})
        totals = totals.sort_values("order").drop(columns="order")
        kpi_row([("Total LLM cost", f"€{totals['total_eur'].sum():.3f}"),
                ("Chat", f"€{totals['chat_eur'].sum():.3f}"),
                ("Automated", f"€{totals['automated_eur'].sum():.3f}")])

        st.subheader("Chat versus automated, by agent (€)")
        st.bar_chart(totals, x="agent_id", y=["chat_eur", "automated_eur"], color=colors(2), horizontal=True, height=320)

        st.subheader("Totals by agent")
        st.dataframe(totals, hide_index=True, width="stretch",
                    column_config={"chat_eur": money("chat_eur"), "automated_eur": money("automated_eur"),
                                   "total_eur": money("total_eur")})

    st.subheader("Today's budget")
    budget_df = pd.DataFrame([{"agent_id": a, "spent_today_eur": round(spent_today(a), 4),
                               "daily_budget_eur": daily_budget(a)} for a in AGENT_ORDER])
    budget_df["progress"] = (budget_df["spent_today_eur"] / budget_df["daily_budget_eur"]).clip(upper=1.0)
    over = budget_df[budget_df["progress"] >= 1.0]
    if not over.empty:
        st.warning(f"{len(over)} agent(s) hit their daily budget today: {', '.join(over['agent_id'])}. "
                   "Their LLM runs are deferred until tomorrow (reflexes keep running).")
    st.dataframe(budget_df, hide_index=True, width="stretch",
                column_config={"spent_today_eur": money("spent_today_eur"), "daily_budget_eur": money("daily_budget_eur"),
                               "progress": st.column_config.ProgressColumn("progress", min_value=0, max_value=1)})

with tab_days:
    days = st.segmented_control("Range", [7, 30, 90], default=30, format_func=lambda d: f"{d} days",
                                key="costs_days") or 30
    by_day = sql("""SELECT date(started_at) AS day,
                           ROUND(SUM(CASE WHEN run_id LIKE 'chat-%' THEN cost_eur ELSE 0 END), 4) AS chat_eur,
                           ROUND(SUM(CASE WHEN run_id NOT LIKE 'chat-%' THEN cost_eur ELSE 0 END), 4) AS automated_eur
                    FROM ops_agent_runs GROUP BY day ORDER BY day""")
    if by_day.empty:
        empty_state("payments", "No agent runs yet")
    else:
        by_day["day"] = pd.to_datetime(by_day["day"])
        by_day = by_day[by_day["day"] >= by_day["day"].max() - pd.Timedelta(days=days)]
        st.subheader("LLM cost per day, chat versus automated (€)")
        st.bar_chart(by_day, x="day", y=["chat_eur", "automated_eur"], color=colors(2), height=300)
        st.dataframe(by_day, hide_index=True, width="stretch",
                    column_config={"chat_eur": money("chat_eur"), "automated_eur": money("automated_eur")})

with tab_conversations:
    convos = sql("""SELECT id, agent_id, channel, started_by, archived, unread, total_cost_eur, created_at, updated_at
                    FROM ops_conversations ORDER BY updated_at DESC LIMIT 200""")
    if convos.empty:
        empty_state("forum", "No conversations yet", "Start one from Chat.")
    else:
        convos["open"] = "/chat?conversation=" + convos["id"]
        st.subheader("Cost per conversation")
        kpi_row([("Conversations", len(convos)), ("Total chat cost", f"€{convos['total_cost_eur'].sum():.3f}")])
        st.dataframe(convos, hide_index=True, width="stretch",
                    column_config={"total_cost_eur": money("total_cost_eur"),
                                   "open": st.column_config.LinkColumn("open", display_text="open")})
