# Accounting > Months tab (J8 period close, J11 immutability): a table of months, closing a month so nothing more
# can be posted into it, verifying a closed month's fingerprint still matches its ledger lines, and reopening the
# single newest closed month for a correction.
#
# `render_periods_tab(by)` is imported and called by accounting.py (not defined there - see that page's module
# docstring). No agent tool can close or reopen a period (see test_periods.py); this tab is the only way.

from __future__ import annotations

import pandas as pd
import streamlit as st

from _common import confirm_dialog, empty_state, step_up
from aecom import periods


def _short(fingerprint: str) -> str:
    return fingerprint[:12] if fingerprint else "\u2014"


def _verified_mark(period: str, status: str) -> str:
    """\u2705/\u274c for a closed month (calls `verify`); nothing to check yet for an open one."""
    if status != "closed":
        return "\u2014"
    return "\u2705" if periods.verify(period)["ok"] else "\u274c"


def _periods_table() -> pd.DataFrame:
    rows = periods.list_periods()
    return pd.DataFrame([{
        "Month": p["period"],
        "Status": p["status"],
        "Ledger lines": p["lines"],
        "Fingerprint": _short(p["fingerprint"]),
        "Verified": _verified_mark(p["period"], p["status"]),
        "Closed by": p["closed_by"],
    } for p in rows])


def _do_close(period: str, by: str, note: str) -> None:
    try:
        result = periods.close(period, by, note)
    except ValueError as exc:  # covers PeriodClosed too (it is a ValueError)
        st.error(str(exc))
        return
    st.success(f"Closed {period}: {result['lines']} ledger lines, fingerprint {_short(result['fingerprint'])}.")


def _close_section(by: str, open_periods: list[str]) -> None:
    st.subheader("Close a month")
    if not open_periods:
        st.caption("No month is open and in the past yet.")
        return
    st.caption("Once a month is closed, nothing more can be posted into it (post the correction into the current "
              "month instead). Only months that have fully passed can be closed, and they close in order.")
    period = st.selectbox("Month", open_periods, key="_period_close_select")
    note = st.text_input("Note (optional)", key="_period_close_note")
    if st.button("Close month", key="_period_close_btn", icon=":material/task_alt:"):
        if not by:
            st.warning("Enter your name in the sidebar first.")
        else:
            confirm_dialog(f"Close {period}?",
                           "Nothing more can be posted into it afterwards; corrections go into the current month "
                           "instead. This records a fingerprint of its ledger lines so later tampering can be "
                           "detected.", lambda: _do_close(period, by, note), confirm_label="Close month")


def _do_reopen(period: str, by: str, reason: str) -> None:
    try:
        periods.reopen(period, by, reason)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(f"Reopened {period}. Close it again once the correction is posted.")


def _reopen_section(by: str, newest_closed: str | None) -> None:
    st.subheader("Reopen the newest closed month")
    if not newest_closed:
        st.caption("No month is closed yet.")
        return
    st.caption(f"Only the single newest closed month ({newest_closed}) can be reopened; an older one stays closed "
              "until it is the newest again. A reason is required and is recorded in the audit log together with "
              "the fingerprint the month had before reopening.")
    if not step_up(f"Confirm your password to reopen {newest_closed}.", key="_period_reopen_stepup"):
        return
    reason = st.text_area("Reason (required)", key="_period_reopen_reason")
    if st.button(f"Reopen {newest_closed}", key="_period_reopen_btn", icon=":material/lock_open:"):
        if not by:
            st.warning("Enter your name in the sidebar first.")
        else:
            _do_reopen(newest_closed, by, reason)


def _verify_all_section() -> None:
    st.subheader("Verify all closed months")
    if st.button("Verify all", key="_period_verify_all_btn", icon=":material/verified:"):
        results = periods.verify_all()
        if not results:
            st.caption("No closed months to verify.")
        elif all(r["ok"] for r in results):
            st.success(f"All {len(results)} closed months match their recorded fingerprint.")
        else:
            bad = [r for r in results if not r["ok"]]
            st.error(f"{len(bad)} of {len(results)} closed months no longer match their recorded fingerprint "
                    "(their ledger lines were changed after closing):")
            for r in bad:
                st.markdown(f"- **{r['period']}**: expected {_short(r['expected'])} "
                           f"({r['lines_expected']} lines), now {_short(r['actual'])} ({r['lines_actual']} lines)")


def render_periods_tab(by: str) -> None:
    st.caption("Every accounting month since the first ledger line. Closing one records a fingerprint of its "
              "ledger lines (J11); any later edit, deletion or backdated insert makes `Verify` fail.")
    rows = periods.list_periods()
    if not rows:
        empty_state("event_busy", "No ledger activity yet", "Months appear here once the ledger has lines.")
        return
    st.dataframe(_periods_table(), hide_index=True, width="stretch")

    open_periods = [p["period"] for p in rows if p["status"] == "open" and periods.is_past(p["period"])]
    closed_periods = sorted((p["period"] for p in rows if p["status"] == "closed"), reverse=True)
    newest_closed = closed_periods[0] if closed_periods else None

    _close_section(by, open_periods)
    st.divider()
    _reopen_section(by, newest_closed)
    st.divider()
    _verify_all_section()
