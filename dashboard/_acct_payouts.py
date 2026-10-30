# Accounting > Payouts tab (F4 payout reconciliation): import a payout export from any platform, see every
# ingested payout with its match status and (if mismatched) its difference, and reconcile one or all pending ones.
# Reconciling books that payout's platform fees once, as a vendor expense (R13), so the P&L carries them.
#
# `render_payouts_tab(by)` is imported and called by accounting.py (not defined there - see that page's module
# docstring).

from __future__ import annotations

import csv
import io

import pandas as pd
import streamlit as st

from _common import empty_state, money, status_badge
from aecom import payouts


def _payouts_table(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Provider": p["provider"], "External id": p["external_id"], "Status": p["status"],
        "Gross": p["gross_eur"], "Fees": p["fees_eur"], "Refunds": p["refunds_eur"], "Net": p["net_eur"],
        "Difference": p["difference_eur"], "Received": p["created_at"],
    } for p in rows])


def _preview(text: str) -> pd.DataFrame | None:
    """The first rows of the uploaded CSV, exactly as the file has them (before any column mapping) - so a human
    can sanity-check it was the right file before importing."""
    try:
        rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    except csv.Error as exc:
        st.error(f"Could not parse the CSV: {exc}")
        return None
    if not rows:
        st.warning("The file has a header row but no data rows.")
        return None
    return pd.DataFrame(rows[:10])


def _import_section(by: str) -> None:
    st.subheader("Import a payout export")
    st.caption("Any platform's payout CSV: common column names (Lemon Squeezy, Stripe, PayPal exports) are "
              "recognised automatically. Re-importing the same payout is safe, it is ignored.")
    provider = st.selectbox("Provider", list(payouts.PROVIDERS), key="_payout_import_provider")
    uploaded = st.file_uploader("Payout CSV", type=["csv"], key="_payout_import_file")
    if uploaded is None:
        return
    try:
        text = uploaded.getvalue().decode("utf-8-sig")
    except UnicodeDecodeError:
        st.error("The file is not valid UTF-8 text.")
        return
    preview = _preview(text)
    if preview is None:
        return
    st.caption("Preview (first 10 rows, as uploaded):")
    st.dataframe(preview, hide_index=True, width="stretch")
    if st.button("Import", key="_payout_import_btn", icon=":material/upload_file:"):
        if not by:
            st.warning("Enter your name in the sidebar first.")
            return
        try:
            result = payouts.import_csv(provider, text, by=by)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.success(f"Imported {result['imported']} payout(s); {result['duplicates']} already existed.")


def _do_reconcile(payout_id: str, by: str) -> None:
    if not by:
        st.warning("Enter your name in the sidebar first.")
        return
    try:
        result = payouts.reconcile(payout_id, by)
    except ValueError as exc:  # covers PeriodClosed too (it is a ValueError)
        st.error(f"{payout_id}: {exc}")
        return
    if result["status"] == "matched":
        st.success(f"{payout_id}: matched.")
    else:
        st.warning(f"{payout_id}: mismatch, difference \u20ac{result['difference_eur']:.2f} (expected minus "
                  "actual net).")


def _do_reconcile_all(payout_ids: list[str], by: str) -> None:
    if not by:
        st.warning("Enter your name in the sidebar first.")
        return
    matched = mismatch = 0
    errors: list[str] = []
    for pid in payout_ids:
        try:
            result = payouts.reconcile(pid, by)
        except ValueError as exc:
            errors.append(f"{pid}: {exc}")
            continue
        if result["status"] == "matched":
            matched += 1
        else:
            mismatch += 1
    if matched:
        st.success(f"{matched} payout(s) matched.")
    if mismatch:
        st.warning(f"{mismatch} payout(s) still mismatch - check their difference below.")
    if errors:
        st.error("; ".join(errors))


def _table_section(by: str) -> None:
    st.subheader("Payouts")
    rows = payouts.list_payouts()
    if not rows:
        empty_state("payments", "No payouts yet", "Import a payout export above to get started.")
        return
    st.dataframe(_payouts_table(rows), hide_index=True, width="stretch",
                column_config={"Gross": money("Gross"), "Fees": money("Fees"), "Refunds": money("Refunds"),
                               "Net": money("Net"), "Difference": money("Difference")})
    for p in rows:
        if p["status"] == "matched":
            continue
        row = st.container(border=True, horizontal=True)
        row.markdown(f"**{p['provider']} {p['external_id']}**")
        with row:
            status_badge(p["status"])
        if row.button("Reconcile", key=f"_payout_reconcile_{p['id']}", icon=":material/fact_check:"):
            _do_reconcile(p["id"], by)

    pending_ids = [p["id"] for p in rows if p["status"] in ("pending", "mismatch")]
    if pending_ids and st.button(f"Reconcile all pending ({len(pending_ids)})", key="_payout_reconcile_all_btn",
                                 icon=":material/playlist_add_check:"):
        _do_reconcile_all(pending_ids, by)


def render_payouts_tab(by: str) -> None:
    st.caption("F4: ingest a payout export from any platform, then reconcile each payout against the orders and "
              "refunds this app mirrored for that period. Reconciling books the platform's fees once, as a "
              "vendor expense, so the P&L carries them (R13).")
    _import_section(by)
    st.divider()
    _table_section(by)
