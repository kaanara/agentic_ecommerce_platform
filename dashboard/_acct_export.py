# Govern > Accounting > Accountant export tab (J10): a general journal CSV or DATEV Buchungsstapel for one closed
# (or at least finished) month, with the best available receipt reference for each expense line.
#
# `render_export_tab(by)` is imported and called by accounting.py (not defined there). All the work - period
# checks, account mapping, receipt lookup, the audit row - lives in `aecom.accountant_export.export_month`; this
# module only renders the month/format pickers and the prepare/download flow.

from __future__ import annotations

import streamlit as st

from _common import empty_state
from aecom import accountant_export
from aecom.db import now

MONTHS_SHOWN = 24


def _recent_finished_periods(n: int = MONTHS_SHOWN) -> list[str]:
    """The last ``n`` months up to (and including) the last one that has already ended."""
    today = now().date()
    year, month = today.year, today.month - 1
    if month == 0:
        year, month = year - 1, 12
    out = []
    for _ in range(n):
        out.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return out


_FORMAT_LABELS = {"datev": "DATEV (Buchungsstapel)", "csv": "General journal (CSV)"}


def render_export_tab(by: str) -> None:
    st.caption("A general journal for one finished month, for your accountant. Account numbers are SKR03 defaults "
              "the accountant must confirm, and this is not tax advice - have them validate the file before using "
              "it, especially the first time.")
    if not by:
        st.warning("Enter your name in the sidebar first.")
        return

    periods = _recent_finished_periods()
    if not periods:
        empty_state("receipt_long", "No finished month yet", "Come back once a full calendar month has passed.")
        return

    period = st.selectbox("Month", periods)
    fmt = st.radio("Format", list(_FORMAT_LABELS), format_func=_FORMAT_LABELS.get, horizontal=True)

    if st.button("Prepare export", type="primary"):
        try:
            result = accountant_export.export_month(period, fmt, by=by)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.session_state["_acct_export_result"] = {"period": period, "fmt": fmt, "result": result}

    prepared = st.session_state.get("_acct_export_result")
    if not prepared:
        return
    result = prepared["result"]
    st.write(f"**{result.rows} row(s)** ready for {prepared['period']} "
            f"({_FORMAT_LABELS.get(prepared['fmt'], prepared['fmt'])}).")
    for warning in result.warnings:
        st.warning(warning)
    mime = "text/csv"
    st.download_button(f"Download {result.filename}", result.content, file_name=result.filename, mime=mime,
                       width="stretch")
