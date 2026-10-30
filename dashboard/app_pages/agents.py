import pandas as pd
import streamlit as st

from _common import AGENT_ORDER, colors, mask_df, require_login, sql
from aecom import run_detail
from aecom.company_settings import agent_enabled, set_agent_enabled

require_login()
st.title("🤖 Agents")


def running_now_panel() -> None:
    """D1: agent runs still marked running; stuck ones (older than run_detail.STUCK_AFTER) are flagged red."""
    st.subheader("Running now")
    rows = run_detail.running_now()
    refreshed = f"Refreshed {pd.Timestamp.now():%H:%M:%S}."
    if not rows:
        st.caption(f"No agent is running. {refreshed}")
        return
    stuck = sum(r["stuck"] for r in rows)
    if stuck:
        st.error(f"{stuck} run(s) running for more than {run_detail.STUCK_AFTER.total_seconds() / 60:.0f} min. "
                 "Open them on Run detail, or use the kill switch on Home.")
    df = pd.DataFrame(rows)
    df.insert(0, "state", df["stuck"].map({True: "🔴 stuck", False: "🟢 running"}))
    df["age_min"] = (df["age_s"] / 60).round(1)
    df["detail"] = "/run?run_id=" + df["run_id"]
    st.dataframe(df[["state", "agent_id", "age_min", "steps", "tool_calls", "cost_eur", "task", "run_id", "started_at",
                     "detail"]], hide_index=True, width="stretch",
                 column_config={"detail": st.column_config.LinkColumn("detail", display_text="open")})
    st.caption(f"{refreshed} Updates every 10 s.")


if hasattr(st, "fragment"):
    st.fragment(run_every=10)(running_now_panel)()
else:  # older Streamlit without fragments: refresh by hand
    running_now_panel()
    st.button("Refresh", key="running_now_refresh")

st.subheader("Enabled agents")
st.caption("A disabled agent's LLM runs are skipped (audited as denied). Reflexes such as order delivery and "
           "restocking keep running. Company runs need the Foreman.")
toggles = st.columns(len(AGENT_ORDER))
for col, agent_id in zip(toggles, AGENT_ORDER):
    on = agent_enabled(agent_id)
    if col.toggle(agent_id, value=on, key=f"agent_enabled_{agent_id}") != on:
        set_agent_enabled(agent_id, not on)
        st.rerun()

runs = sql("""SELECT agent_id, COUNT(*) AS runs, SUM(tool_calls) AS tool_calls, SUM(input_tokens) AS input_tokens,
                     SUM(output_tokens) AS output_tokens, ROUND(SUM(cost_eur), 4) AS cost_eur,
                     SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                     SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS not_ok
              FROM ops_agent_runs GROUP BY agent_id""")
decisions = sql("SELECT agent_id, decision, COUNT(*) AS n FROM ops_tool_calls GROUP BY agent_id, decision")

if runs.empty:
    st.info("No agent runs yet. Try `aecom scenario all --offline`.")
    st.stop()

runs["order"] = runs["agent_id"].map({a: i for i, a in enumerate(AGENT_ORDER)})
runs = runs.sort_values("order").drop(columns="order")

c = st.columns(4)
c[0].metric("Agent runs", int(runs["runs"].sum()))
c[1].metric("Tool calls", int(runs["tool_calls"].sum()))
c[2].metric("LLM cost", f"€{runs['cost_eur'].sum():.3f}")
esc = int(decisions.loc[decisions["decision"] == "escalate", "n"].sum()) if not decisions.empty else 0
c[3].metric("Escalations", esc)

left, right = st.columns(2)
with left:
    st.subheader("LLM cost by agent (€)")
    st.bar_chart(runs, x="agent_id", y="cost_eur", color=colors(1)[0], horizontal=True, height=320)
with right:
    st.subheader("Policy decisions by agent")
    if not decisions.empty:
        wide = decisions.pivot_table(index="agent_id", columns="decision", values="n", fill_value=0).reset_index()
        cols = [x for x in ["allow", "escalate", "deny"] if x in wide.columns]  # fixed order → fixed colours
        st.bar_chart(wide, x="agent_id", y=cols, color=colors(len(cols)), horizontal=True, height=320)

st.subheader("Per-agent summary")
st.dataframe(runs, hide_index=True, width="stretch")

st.subheader("Most used tools")
st.dataframe(sql("""SELECT tool_name, agent_id, COUNT(*) AS calls, SUM(CASE WHEN ok THEN 0 ELSE 1 END) AS errors
                    FROM ops_tool_calls GROUP BY tool_name, agent_id ORDER BY calls DESC LIMIT 25"""),
             hide_index=True, width="stretch")

st.subheader("Recent runs")
agent = st.selectbox("Agent", ["all", *AGENT_ORDER])
recent = sql("""SELECT started_at, run_id, agent_id, model, status, steps, tool_calls, ROUND(cost_eur, 4) AS cost_eur,
                       substr(task, 1, 160) AS task, output
                FROM ops_agent_runs WHERE (:a = 'all' OR agent_id = :a) ORDER BY started_at DESC LIMIT 50""", a=agent)
st.dataframe(recent.drop(columns="output"), hide_index=True, width="stretch")
for _, r in recent.head(5).iterrows():
    with st.expander(f"{r['agent_id']} · {r['status']} · {r['started_at']}"):
        st.markdown(f"**Task:** {r['task']}")
        st.code(r["output"] or "", language="markdown")

st.subheader("Registry: tools, skills and MCP servers")
st.dataframe(sql("""SELECT t.name, t.kind, t.status, t.version, t.proposed_by, t.approved_by,
                           GROUP_CONCAT(a.agent_id) AS agents
                    FROM registry_tools t LEFT JOIN registry_allowlist a ON a.tool_name = t.name
                    WHERE t.kind <> 'builtin' OR a.agent_id IS NOT NULL
                    GROUP BY t.name, t.kind, t.status, t.version, t.proposed_by, t.approved_by ORDER BY t.kind, t.name"""),
             hide_index=True, width="stretch")

st.subheader("Proposed lessons (procedural memory)")
st.dataframe(sql("SELECT created_at, agent_id, lesson, evidence, status FROM memory_lessons ORDER BY created_at DESC"),
             hide_index=True, width="stretch")

st.subheader("Audit trail")
audit_trail = sql("SELECT ts, actor, action, decision, run_id, detail FROM ops_audit_log ORDER BY id DESC LIMIT 100")
st.dataframe(mask_df(audit_trail, ["detail"]), hide_index=True, width="stretch")
