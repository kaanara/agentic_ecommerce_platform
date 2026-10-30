# Operate > Overview: a live status banner, a KPI row, what needs attention, recent activity and shortcuts.
import streamlit as st

from _common import confirm_dialog, empty_state, kpi_row, mask_df, page_header, require_login, reviewer_name, sql
from aecom import approvals, incidents
from aecom import dashboard_auth
from aecom.company_settings import agents_paused_on, get_setting, kill_switch_on, leave_safe_mode, safe_mode_on, set_flag
from aecom.conversations import unread_total

require_login()
page_header("Overview", "dashboard",
           "Digital products, operated by agents, governed by policy, approved by humans.")

if dashboard_auth.disabled() or not dashboard_auth.is_configured():
    with st.container(border=True):
        st.markdown("#### :material/lock_open: Secure your dashboard")
        st.caption("Right now anyone who can reach this page can approve spending and use the kill switch, and Chat "
                   "stays off. Set a password (12 or more characters) to turn the login on.")
        st.page_link("app_pages/settings.py", label="Set a password", icon=":material/lock:")


def _leave_safe_mode(by: str) -> None:
    leave_safe_mode(by)
    st.rerun()


def _set_kill_switch(on: bool, by: str) -> None:
    set_flag("kill_switch", on, by or "dashboard")
    st.rerun()


def _set_paused(on: bool, by: str) -> None:
    set_flag("agents_paused", on, by or "dashboard")
    st.rerun()


def _status_banner() -> None:
    """The live part of the page: safe mode, the kill switch, pending approvals, open incidents and unread chat.
    Kill switch, pause and leaving safe mode are all behind a confirmation dialog - nothing fires on a bare click."""
    reviewer = reviewer_name()
    kill = kill_switch_on()
    paused = agents_paused_on()
    pending = len(approvals.list_approvals("pending"))
    open_total = sum(incidents.open_counts().values())
    unread = unread_total()

    if safe_mode_on():
        st.error(f"**Safe mode is ON** since {get_setting('safe_mode_since') or '?'}: "
                f"{get_setting('safe_mode_reason') or 'no reason recorded'}. Only deliveries and customer replies "
                "continue; nothing that spends money runs. Acknowledge the incident on Health, fix it, then leave "
                "safe mode here. The monitors never leave it on their own.", icon=":material/warning:")
        if st.button("Leave safe mode", type="primary", disabled=not reviewer, key="leave_safe_mode"):
            confirm_dialog("Leave safe mode?",
                          "Agents resume spending money again. Make sure the underlying problem is actually fixed "
                          "first.", lambda: _leave_safe_mode(reviewer), confirm_label="Leave safe mode")
        if not reviewer:
            st.caption("Enter your name in the sidebar to leave safe mode.")

    kpi_row([("Kill switch", "ON" if kill else "off"), ("Agents", "paused" if paused else "running"),
            ("Pending approvals", pending), ("Open incidents", open_total), ("Unread chats", unread)])

    row = st.container(horizontal=True)
    if row.button("Turn kill switch OFF" if kill else "Turn kill switch ON",
                 type="secondary" if kill else "primary", disabled=not reviewer, key="toggle_kill",
                 help="Emergency stop: blocks every side effect, including key delivery to paying customers."):
        if kill:
            confirm_dialog("Turn the kill switch off?", "Agents and reflexes resume every action immediately.",
                          lambda: _set_kill_switch(False, reviewer), confirm_label="Turn off")
        else:
            confirm_dialog("Turn the kill switch ON?",
                          "Blocks every side effect, including key delivery to paying customers, until turned off "
                          "again here.", lambda: _set_kill_switch(True, reviewer), confirm_label="Turn on",
                          type_to_confirm="KILL")
    if row.button("Resume agents" if paused else "Pause agents", disabled=not reviewer, key="toggle_pause",
                 help="Agents and reflexes stop taking actions, but paid orders are still delivered "
                      "(policy.yaml -> continue_while_paused). Use the kill switch to stop everything."):
        if paused:
            confirm_dialog("Resume agents?", "Agents and reflexes start taking actions again.",
                          lambda: _set_paused(False, reviewer), confirm_label="Resume")
        else:
            confirm_dialog("Pause agents?",
                          "Agents and reflexes stop taking actions; paid orders are still delivered.",
                          lambda: _set_paused(True, reviewer), confirm_label="Pause")
    if not reviewer:
        st.caption("Enter your name in the sidebar to use these controls.")


if hasattr(st, "fragment"):
    st.fragment(run_every=10)(_status_banner)()
else:  # older Streamlit without fragments: refresh by hand
    _status_banner()
    st.button("Refresh", key="overview_refresh")

st.divider()
orders = int(sql("SELECT COUNT(*) AS n FROM commerce_orders")["n"][0])
runs = int(sql("SELECT COUNT(*) AS n FROM ops_agent_runs")["n"][0])
decisions = int(sql("SELECT COUNT(*) AS n FROM company_decisions")["n"][0])
kpi_row([("Orders", orders), ("Agent runs", runs), ("Decisions (ADRs)", decisions)])

st.subheader("Needs attention")
ATTENTION_ICON = "warning"


def _attention_card(icon: str, title: str, hint: str, link: str | None = None) -> None:
    with st.container(border=True):
        row = st.container(horizontal=True)
        row.markdown(f"**:material/{icon}: {title}**")
        if link:
            row.page_link(link, label="Open")
        st.caption(hint)


items: list[tuple[str, str, str, str | None]] = []
if safe_mode_on():
    items.append((ATTENTION_ICON, "Safe mode is ON", "Fix the underlying incident, then leave safe mode above.", None))
if kill_switch_on():
    items.append((ATTENTION_ICON, "Kill switch is ON", "Every side effect is blocked, including customer deliveries.",
                 None))
if agents_paused_on():
    items.append(("pause_circle", "Agents are paused", "Reflex deliveries continue; agents take no new actions.", None))
pending_n = len(approvals.list_approvals("pending"))
if pending_n:
    items.append(("fact_check", f"{pending_n} approval(s) waiting", "Review and decide on the Approvals page.",
                 "app_pages/approvals.py"))
open_n = sum(incidents.open_counts().values())
if open_n:
    items.append(("monitor_heart", f"{open_n} open incident(s)", "Acknowledge or resolve them on Health.",
                 "app_pages/health.py"))
unread_n = unread_total()
if unread_n:
    items.append(("forum", f"{unread_n} unread message(s)", "Reply on the Chat page.", "app_pages/chat.py"))

if not items:
    empty_state("task_alt", "Nothing needs attention", "Safe mode and the kill switch are off, agents are running, "
               "and there are no pending approvals, open incidents or unread messages.")
else:
    for icon, title, hint, link in items:
        _attention_card(icon, title, hint, link)

st.subheader("Recent activity")
audit_trail = sql("SELECT ts, actor, action, decision, detail FROM ops_audit_log ORDER BY id DESC LIMIT 15")
if audit_trail.empty:
    empty_state("history", "No activity recorded yet")
else:
    st.dataframe(mask_df(audit_trail, ["detail"]), hide_index=True, width="stretch")

st.subheader("Shortcuts")
links = st.container(horizontal=True)
links.page_link("app_pages/chat.py", label="Chat", icon=":material/forum:")
links.page_link("app_pages/approvals.py", label="Approvals", icon=":material/fact_check:")
links.page_link("app_pages/business.py", label="Business", icon=":material/insights:")
links.page_link("app_pages/health.py", label="Health", icon=":material/monitor_heart:")
links.page_link("app_pages/settings.py", label="Settings", icon=":material/settings:")
