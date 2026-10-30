import pandas as pd
import streamlit as st

from _common import require_login
from aecom import trust

require_login()
st.title("🤝 Trust")
st.caption("Autonomy is earned per action and lost automatically. A promotion raises only the per-call limits of one "
           "action by one stage, needs a named human and recorded evidence, and is re-checked on the server. "
           "**Hard caps never move**: monthly budgets, the recipient rule, T3/T4 rules, kill switch, pause and safe "
           "mode stay exactly as they are. A demotion always goes back to stage 1.")


def pct(v: float | None) -> str:
    return "-" if v is None else f"{v:.0%}"


rows = trust.report()
st.dataframe(pd.DataFrame([{"action": r["tool"], "stage": r["stage"], "escalations": r["stats"]["escalations"],
                            "unchanged rate": pct(r["stats"]["unchanged_rate"]),
                            "reject rate": pct(r["stats"]["reject_rate"]),
                            "eligible": r["eligible"], "missing": "; ".join(r["missing"])} for r in rows]),
             hide_index=True, width="stretch")
st.caption(f"Real sales so far: {trust.real_sales()}. Thresholds: "
           + ", ".join(f"{k.removeprefix('AUTONOMY_').lower()} {v:g}" for k, v in trust.thresholds().items()))

st.subheader("Promote or demote")
tool = st.selectbox("Action", [r["tool"] for r in rows])
name = st.text_input("Your name (required)")
reason = st.text_input("Reason")
c1, c2, _ = st.columns([1, 1, 4])
if c1.button("Promote one stage"):
    try:
        st.success(f"{tool} is now stage {trust.promote(tool, name, reason)}")
    except ValueError as e:
        st.error(str(e))
if c2.button("Demote to stage 1"):
    try:
        st.success(f"{tool} is now stage {trust.demote(tool, name, reason)}")
    except ValueError as e:
        st.error(str(e))

with st.expander("Autonomous actions and stage changes (last 7 days)"):
    st.dataframe(pd.DataFrame(trust.log(7), columns=["ts", "kind", "tool", "actor", "detail"]),
                 hide_index=True, width="stretch")
