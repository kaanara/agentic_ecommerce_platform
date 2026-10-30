import pandas as pd
import streamlit as st

from _common import confirm_dialog, empty_state, page_header, reviewer_name, require_login
from aecom import trust

require_login()
page_header("Trust", "verified_user",
            "Autonomy is earned per action and lost automatically. A promotion raises only the per-call limits of "
            "one action by one stage, needs a named human and recorded evidence, and is re-checked on the server. "
            "**Hard caps never move**: monthly budgets, the recipient rule, T3/T4 rules, kill switch, pause and "
            "safe mode stay exactly as they are. A demotion always goes back to stage 1.")


def pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


def _do_promote(tool: str, name: str, reason: str) -> None:
    try:
        stage = trust.promote(tool, name, reason)
    except ValueError as e:
        st.error(str(e))
        return
    st.success(f"{tool} is now stage {stage}.")
    st.rerun()


def _do_demote(tool: str, name: str, reason: str) -> None:
    try:
        stage = trust.demote(tool, name, reason)
    except ValueError as e:
        st.error(str(e))
        return
    st.success(f"{tool} is now stage {stage}.")
    st.rerun()


rows = trust.report()

tab_report, tab_log = st.tabs(["Report", "Autonomous actions (7d)"])

with tab_report:
    st.dataframe(pd.DataFrame([{"action": r["tool"], "stage": r["stage"], "escalations": r["stats"]["escalations"],
                                "unchanged rate": pct(r["stats"]["unchanged_rate"]),
                                "reject rate": pct(r["stats"]["reject_rate"]),
                                "eligible": r["eligible"], "missing": "; ".join(r["missing"])} for r in rows]),
                 hide_index=True, width="stretch")
    st.caption(f"Real sales so far: {trust.real_sales()}. Thresholds: "
               + ", ".join(f"{k.removeprefix('AUTONOMY_').lower()} {v:g}" for k, v in trust.thresholds().items()))

    st.subheader("Promote or demote")
    tool = st.selectbox("Action", [r["tool"] for r in rows])
    name = reviewer_name()
    if not name:
        st.caption("Enter your name in the sidebar before changing a stage.")
    reason = st.text_input("Reason")
    actions = st.container(horizontal=True)
    if actions.button("Promote one stage", disabled=not name):
        confirm_dialog(f"Promote {tool} one stage?",
                       "The server re-checks the evidence gate before applying this; a stale form can't force it "
                       "through.",
                       lambda: _do_promote(tool, name, reason), confirm_label="Promote")
    if actions.button("Demote to stage 1", disabled=not name):
        confirm_dialog(f"Demote {tool} to stage 1?",
                       "This immediately lowers the per-call limits back to the starting stage.",
                       lambda: _do_demote(tool, name, reason), confirm_label="Demote", type_to_confirm=tool)

with tab_log:
    log_rows = trust.log(7)
    if not log_rows:
        empty_state("history", "No stage changes or autonomous actions in the last 7 days")
    else:
        st.dataframe(pd.DataFrame(log_rows, columns=["ts", "kind", "tool", "actor", "detail"]),
                     hide_index=True, width="stretch")
