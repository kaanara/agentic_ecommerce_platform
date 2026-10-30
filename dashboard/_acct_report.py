# Govern > Accounting > Month-end report tab (J9): P&L, cash movement, VAT per country and rate, budget vs
# actual, top products, refunds, open incidents, unmatched ledger items and the (not yet built) payout
# reconciliation, for one calendar month.
#
# `render_month_end_tab(by)` is imported and called by accounting.py (not defined there - see that page's module
# docstring). All the numbers come from `aecom.month_end`; this module only renders the month picker, the numbers
# preview, the create/download flow and the list of versions already stored, like the other accounting tabs.

from __future__ import annotations

import streamlit as st

from _common import empty_state
from aecom import month_end, periods, reports

KIND = "month_end"


def _months_with_ledger_lines() -> list[str]:
    """Every month that has ledger lines so far, newest first (including the current, still-open one - picking it
    just previews the numbers; ``Create report`` is what refuses it)."""
    return sorted({p["period"] for p in periods.list_periods()}, reverse=True)


def _reports_for(period: str) -> list[dict]:
    return [r for r in reports.list_reports(KIND) if (r["period_start"] or "")[:7] == period]


def _do_create(period: str, by: str) -> None:
    try:
        report = month_end.generate(period, by)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(f"Month-end report for {period} created (version {report['version']}).")


def render_month_end_tab(by: str) -> None:
    st.caption("P&L, cash movement, VAT per country and rate, budget vs actual, top products, refunds, open "
              "incidents and unmatched ledger items for one calendar month. Payout reconciliation is not built "
              "yet (F4), so that section just lists what is open.")
    months = _months_with_ledger_lines()
    if not months:
        empty_state("event_busy", "No ledger activity yet", "A month appears here once it has ledger lines.")
        return

    period = st.selectbox("Month", months, key="_month_end_select")
    data = month_end.build(period)

    st.subheader("Preview")
    st.markdown(month_end.numbers_text(data))

    if not by:
        st.warning("Enter your name in the sidebar first (on a phone, tap the \u00bb at the top left). It is recorded in the audit log.")
    elif st.button("Create report", key="_month_end_create_btn", type="primary", icon=":material/fact_check:"):
        _do_create(period, by)

    st.subheader("Reports for this month")
    rows = _reports_for(period)
    if not rows:
        st.caption("No month-end report stored for this month yet.")
        return

    labels = {r["id"]: f"v{r['version']} \u00b7 {r['created_by']} \u00b7 {r['created_at']}" for r in rows}
    chosen = st.selectbox("Version", list(labels), format_func=labels.get, key="_month_end_version_select")
    report = next(r for r in rows if r["id"] == chosen)
    st.text(report["text"])

    stem = f"month_end_{period}_v{report['version']}"
    downloads = st.container(horizontal=True)
    downloads.download_button("Markdown", reports.to_markdown(report), file_name=f"{stem}.md",
                              mime="text/markdown", width="stretch")
    downloads.download_button("P&L CSV", month_end.pnl_csv(report["data"]), file_name=f"{stem}_pnl.csv",
                              mime="text/csv", width="stretch")
