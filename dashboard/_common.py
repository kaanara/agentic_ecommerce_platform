"""Shared helpers for the Streamlit dashboards (run with `uv run aecom dashboard`)."""

from __future__ import annotations

import json
from typing import Any, Iterable

import pandas as pd
import streamlit as st
from sqlalchemy import text

import aecom.handlers  # noqa: F401  (approvals publish events handled in-process)
from aecom import dashboard_auth
from aecom.audit import mask_detail
from aecom.bootstrap import init
from aecom.db import get_engine

# Reference categorical palette (dataviz skill), fixed order — never cycled. Slots 1-3 are safe for any chart form.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
AGENT_ORDER = ["ceo", "researcher", "judge", "foreman", "finance", "store_ops", "marketing", "support", "supply"]


def require_login() -> None:
    """Gate every page: call right after st.set_page_config, before any query or button. Fails closed."""
    if dashboard_auth.disabled():
        st.warning("⚠️ Dashboard login is disabled (DASHBOARD_AUTH=off). Anyone who can reach this page can approve "
                   "actions and use the kill switch. Keep it off your network, or remove DASHBOARD_AUTH=off from .env.")
        _boot()
        return
    if not dashboard_auth.is_configured():
        st.error("Dashboard login is not configured, so nothing is shown.")
        st.markdown(f"Run `uv run python -m aecom.dashboard_auth`, paste the printed `{dashboard_auth.ENV_VAR}=...` "
                    "line into `.env`, and restart `uv run aecom dashboard`.")
        st.stop()
    if not st.session_state.get("authenticated"):
        st.title("🔒 Log in")
        wait = dashboard_auth.locked_out(st.session_state)
        with st.form("login"):
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", disabled=wait > 0)
        if wait > 0:
            st.warning(f"Too many wrong attempts. Try again in {wait:.0f} s.")
        elif submitted:
            if dashboard_auth.attempt_login(password, st.session_state):
                st.rerun()
            st.error("Wrong password.")
        st.stop()
    if st.sidebar.button("Log out"):
        st.session_state["authenticated"] = False
        st.rerun()
    _boot()  # pages may call domain modules (approvals, ledger) before their first sql()


@st.cache_resource
def _boot() -> bool:
    init()
    return True


def sql(query: str, **params: object) -> pd.DataFrame:
    _boot()
    with get_engine().connect() as conn:
        return pd.read_sql(text(query), conn, params=params)


def colors(n: int) -> list[str]:
    return SERIES[:n]


def _mask_cell(value: Any) -> Any:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return mask_detail(value)
        return json.dumps(mask_detail(parsed), ensure_ascii=False) if isinstance(parsed, (dict, list)) else mask_detail(value)
    if isinstance(value, (dict, list)):
        return mask_detail(value)
    return value


def mask_df(df: pd.DataFrame, columns: Iterable[str]) -> pd.DataFrame:
    """Copy of ``df`` with the given columns secret-masked (I16) - for tables from ops_audit_log, ops_tool_calls and
    ops_outbox_emails bodies. The stored rows are unchanged (the outbox keeps the full emails actually sent)."""
    out = df.copy()
    for col in columns:
        if col in out.columns:
            out[col] = out[col].map(_mask_cell)
    return out
