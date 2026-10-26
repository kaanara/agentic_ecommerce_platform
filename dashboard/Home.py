import streamlit as st

from _common import require_login, sql
from aecom.company_settings import agents_paused_on, get_setting, kill_switch_on, leave_safe_mode, safe_mode_on, set_setting

st.set_page_config(page_title="aecom — agentic company", page_icon="🧭", layout="wide")
require_login()
st.title("🧭 Agentic e-commerce company")
st.caption("Digital products, operated by agents, governed by policy, approved by humans.")

if safe_mode_on():
    st.error(f"**Safe mode is ON** since {get_setting('safe_mode_since') or '?'}: "
             f"{get_setting('safe_mode_reason') or 'no reason recorded'}. Only deliveries and customer replies "
             "continue; nothing that spends money runs. Acknowledge the incident (Incidents / `aecom incidents ack`), "
             "fix it, then leave safe mode here. The monitors never leave it on their own.", icon="🚨")
    who = st.text_input("Your name (recorded in the audit log)", key="safe_mode_by")
    if st.button("Leave safe mode", type="primary", disabled=not who.strip()):
        leave_safe_mode(who)
        st.rerun()

pending = int(sql("SELECT COUNT(*) AS n FROM ops_approvals WHERE status = 'pending'")["n"][0])
runs = int(sql("SELECT COUNT(*) AS n FROM ops_agent_runs")["n"][0])
orders = int(sql("SELECT COUNT(*) AS n FROM commerce_orders")["n"][0])

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Pending approvals", pending)
c2.metric("Agent runs", runs)
c3.metric("Orders", orders)
with c4:
    on = kill_switch_on()
    st.metric("Kill switch", "ON 🛑" if on else "off ✅")
    if st.button("Turn OFF" if on else "Turn ON (block all actions)", type="secondary" if on else "primary"):
        set_setting("kill_switch", "off" if on else "on")
        st.rerun()
    st.caption("Emergency stop: blocks every side effect, including key delivery to paying customers.")
with c5:
    paused = agents_paused_on()
    st.metric("Agents", "PAUSED ⏸️" if paused else "running ✅")
    if st.toggle("Pause agents (deliveries continue)", value=paused,
                 help="Agents and reflexes stop taking actions, but paid orders are still delivered "
                      "(policy.yaml → continue_while_paused). Use the kill switch to stop everything.") != paused:
        set_setting("agents_paused", "off" if paused else "on")
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
