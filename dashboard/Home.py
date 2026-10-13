import streamlit as st

from _common import sql
from aecom.company_settings import kill_switch_on, set_setting

st.set_page_config(page_title="aecom — agentic company", page_icon="🧭", layout="wide")
st.title("🧭 Agentic e-commerce company")
st.caption("Digital products, operated by agents, governed by policy, approved by humans.")

pending = int(sql("SELECT COUNT(*) AS n FROM ops_approvals WHERE status = 'pending'")["n"][0])
runs = int(sql("SELECT COUNT(*) AS n FROM ops_agent_runs")["n"][0])
orders = int(sql("SELECT COUNT(*) AS n FROM commerce_orders")["n"][0])

c1, c2, c3, c4 = st.columns(4)
c1.metric("Pending approvals", pending)
c2.metric("Agent runs", runs)
c3.metric("Orders", orders)
with c4:
    on = kill_switch_on()
    st.metric("Kill switch", "ON 🛑" if on else "off ✅")
    if st.button("Turn OFF" if on else "Turn ON (block all actions)", type="secondary" if on else "primary"):
        set_setting("kill_switch", "off" if on else "on")
        st.rerun()

st.markdown("""
**Pages**
- **Business**: revenue, margin, P&L, OKRs, campaigns and ROAS
- **Agents**: runs, cost, tool usage, policy decisions and the audit trail
- **Approvals**: the human-in-the-loop queue (approve, edit or reject)
""")

st.subheader("Recent decisions (ADRs)")
st.dataframe(sql("SELECT created_at, category, choice, title, decided_by, status FROM company_decisions ORDER BY created_at DESC LIMIT 10"),
             hide_index=True, width="stretch")
st.subheader("Recent events")
st.dataframe(sql("SELECT ts, topic, source, processed FROM ops_events ORDER BY id DESC LIMIT 15"), hide_index=True,
             width="stretch")
