import pandas as pd
import streamlit as st

from _common import (
    AGENT_ORDER,
    agent_label,
    colors,
    empty_state,
    kpi_row,
    mask_df,
    page_header,
    require_login,
    sql,
)
from aecom import run_detail
from aecom.company_settings import agent_enabled, set_agent_enabled
from aecom.persona.loader import load_spec

require_login()
page_header("Agents", "smart_toy", "Who is running right now, what every agent has done, and what each persona is "
           "configured to do.")

tab_live, tab_runs, tab_prompts = st.tabs(["Live", "Runs", "Prompts"])


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


with tab_live:
    if hasattr(st, "fragment"):
        st.fragment(run_every=10)(running_now_panel)()
    else:  # older Streamlit without fragments: refresh by hand
        running_now_panel()
        st.button("Refresh", key="running_now_refresh")

    st.subheader("Enabled agents")
    st.caption("A disabled agent's LLM runs are skipped (audited as denied). Reflexes such as order delivery and "
              "restocking keep running. Company runs need the Foreman.")
    toggles = st.container(horizontal=True)
    for agent_id in AGENT_ORDER:
        on = agent_enabled(agent_id)
        if toggles.toggle(agent_label(agent_id), value=on, key=f"agent_enabled_{agent_id}") != on:
            set_agent_enabled(agent_id, not on)
            st.rerun()

with tab_runs:
    runs = sql("""SELECT agent_id, COUNT(*) AS runs, SUM(tool_calls) AS tool_calls, SUM(input_tokens) AS input_tokens,
                         SUM(output_tokens) AS output_tokens, ROUND(SUM(cost_eur), 4) AS cost_eur,
                         SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok,
                         SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS not_ok
                  FROM ops_agent_runs GROUP BY agent_id""")
    decisions = sql("SELECT agent_id, decision, COUNT(*) AS n FROM ops_tool_calls GROUP BY agent_id, decision")

    if runs.empty:
        empty_state("smart_toy", "No agent runs yet", "Try `aecom scenario all --offline`.")
    else:
        runs["order"] = runs["agent_id"].map({a: i for i, a in enumerate(AGENT_ORDER)})
        runs = runs.sort_values("order").drop(columns="order")

        esc = int(decisions.loc[decisions["decision"] == "escalate", "n"].sum()) if not decisions.empty else 0
        kpi_row([("Agent runs", int(runs["runs"].sum())), ("Tool calls", int(runs["tool_calls"].sum())),
                ("LLM cost", f"€{runs['cost_eur'].sum():.3f}"), ("Escalations", esc)])

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

    st.subheader("Audit trail")
    audit_trail = sql("SELECT ts, actor, action, decision, run_id, detail FROM ops_audit_log ORDER BY id DESC LIMIT 100")
    if audit_trail.empty:
        empty_state("history", "No audit entries yet")
    else:
        st.dataframe(mask_df(audit_trail, ["detail"]), hide_index=True, width="stretch")

with tab_prompts:
    st.subheader("Persona")
    chosen = st.selectbox("Agent", AGENT_ORDER, format_func=agent_label, key="prompts_agent")
    spec = load_spec(chosen)
    kpi_row([("Model tier", spec.model_tier),
            ("Budget / day", f"€{spec.budget_eur_per_day:.2f}" if spec.budget_eur_per_day else "—"),
            ("Reports to", spec.reports_to or "—"), ("Tools", len(spec.tools)),
            ("Server tools", len(spec.server_tools))])
    st.caption(f"Role: {spec.role}")
    if spec.tools or spec.server_tools:
        st.caption("Allowed tools: " + ", ".join([*spec.tools, *spec.server_tools]))
    with st.expander("Soul (identity & values)"):
        st.markdown(spec.soul or "—")
    with st.expander("Procedures (AGENTS.md)"):
        st.markdown(spec.procedures or "—")
    st.caption("Prompts are edited in Settings > Agents (overrides, never the tracked file directly).")

    st.subheader("Registry: tools, skills and MCP servers")
    st.dataframe(sql("""SELECT t.name, t.kind, t.status, t.version, t.proposed_by, t.approved_by,
                               GROUP_CONCAT(a.agent_id) AS agents
                        FROM registry_tools t LEFT JOIN registry_allowlist a ON a.tool_name = t.name
                        WHERE t.kind <> 'builtin' OR a.agent_id IS NOT NULL
                        GROUP BY t.name, t.kind, t.status, t.version, t.proposed_by, t.approved_by ORDER BY t.kind, t.name"""),
                 hide_index=True, width="stretch")

    st.subheader("Proposed lessons (procedural memory)")
    lessons = sql("SELECT created_at, agent_id, lesson, evidence, status FROM memory_lessons ORDER BY created_at DESC")
    if lessons.empty:
        empty_state("lightbulb", "No proposed lessons yet")
    else:
        st.dataframe(lessons, hide_index=True, width="stretch")
