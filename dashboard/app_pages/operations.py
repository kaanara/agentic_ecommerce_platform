# Govern > Operations: the actions that used to be CLI-only (company runs, store sync, reconcile, monitors, memory
# review, tool grants, vendor provisioning, incident reports). Each tab is a partial module; every state change goes
# through the same service functions the CLI uses. Heavy work is queued as a job and run by the scheduler.
import streamlit as st

from _common import page_header, require_login, reviewer_name

require_login()
page_header("Operations", "build",
           "Everything you could only do from the command line: run the company, sync the store, review shared "
           "memories, manage tool grants and vendor sign-ups, write incident reports, and connect ad and marketplace accounts.")
by = reviewer_name()
if not by:
    st.info("Enter your name in the sidebar to use the actions on this page (it is recorded in the audit log).")

TABS = [("Run and maintenance", "_ops_actions", "render_actions_tab"),
        ("Memory review", "_ops_memory", "render_memory_tab"),
        ("Tool grants", "_ops_registry", "render_grants_tab"),
        ("Vendor provisioning", "_ops_provisioning", "render_provisioning_tab"),
        ("Incident reports", "_ops_incidents", "render_incident_reports_tab"),
        ("Connections", "_ops_connections", "render_connections_tab")]

for tab, (label, module, func) in zip(st.tabs([t[0] for t in TABS]), TABS):
    with tab:
        try:
            render = getattr(__import__(module), func)
        except (ImportError, AttributeError):
            st.caption(f"{label} is not available in this build.")
            continue
        render(by)
