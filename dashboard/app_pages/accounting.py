# Govern > Accounting: close months, month-end report, accountant export and the quarterly look back at decisions.
# Each tab is a partial module; only humans can close or reopen a month (no agent tool exists for it).
import streamlit as st

from _common import page_header, require_login, reviewer_name

require_login()
page_header("Accounting", "account_balance",
           "Close a month so nothing can be posted into it, produce the month-end report, hand the books to your "
           "accountant, and review whether past decisions delivered.")
by = reviewer_name()
if not by:
    st.info("Enter your name in the sidebar to close months, create reports or save reviews (it is recorded in the "
            "audit log).")

TABS = [("Months", "_acct_periods", "render_periods_tab"),
        ("Month-end report", "_acct_report", "render_month_end_tab"),
        ("Accountant export", "_acct_export", "render_export_tab"),
        ("Decision review", "_acct_decisions", "render_decision_review_tab")]

for tab, (label, module, func) in zip(st.tabs([t[0] for t in TABS]), TABS):
    with tab:
        try:
            render = getattr(__import__(module), func)
        except (ImportError, AttributeError):
            st.caption(f"{label} is not available in this build.")
            continue
        render(by)
