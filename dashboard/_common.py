"""Shared helpers for the Streamlit dashboards (run with `uv run aecom dashboard`)."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, NamedTuple

import pandas as pd
import streamlit as st
from sqlalchemy import text

import aecom.handlers  # noqa: F401  (approvals publish events handled in-process)
from aecom import dashboard_auth
from aecom.audit import mask_detail
from aecom.bootstrap import init
from aecom.db import get_engine

# Reference categorical palette (dataviz skill), fixed order — never cycled. Slots 1-3 are safe for any chart form.
# Keep in step with chartCategoricalColors in .streamlit/config.toml.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def _agent_order() -> list[str]:
    from aecom.persona.loader import agent_ids

    preferred = ["ceo", "researcher", "judge", "foreman", "finance", "store_ops", "marketing", "support", "supply", "sentinel"]
    ids = list(agent_ids())
    return [a for a in preferred if a in ids] + [a for a in ids if a not in preferred]


AGENT_ORDER = _agent_order()


class Kpi(NamedTuple):
    label: str
    value: Any
    delta: str | None = None
    help: str | None = None


def _first_gate_call_this_run() -> bool:
    """True the first time require_login() runs in a script run. The entrypoint (Home.py) and every page call it, so
    the second call must stay silent (no duplicate warning, no duplicate widget)."""
    from streamlit.runtime.scriptrunner import get_script_run_ctx

    ctx = get_script_run_ctx()
    marker = id(ctx) if ctx is not None else None
    if marker is not None and st.session_state.get("_gate_run") == marker:
        return False
    st.session_state["_gate_run"] = marker
    return True


def require_login() -> None:
    """Gate every page: call right after st.set_page_config (entrypoint) or first thing in a page, before any query or
    button. Safe to call twice per run (entrypoint + page). Fails closed."""
    first = _first_gate_call_this_run()
    if dashboard_auth.disabled():
        if first:
            st.warning("Dashboard login is off. Anyone who can reach this page can approve actions and use the kill "
                       "switch. Chat is disabled while the login is off. Turn it on in Settings > Security.",
                       icon=":material/lock_open:")
        _boot()
        return
    if not dashboard_auth.is_configured():
        st.error("No dashboard password is set yet, so no one can log in.")
        st.markdown("Run `uv run aecom dashboard reset-password` on the machine to create one, then reload this "
                    f"page. (The older way still works: set `{dashboard_auth.ENV_VAR}` in `.env`.)")
        st.stop()
    if not st.session_state.get("authenticated"):
        st.title(":material/lock: Log in")
        wait = dashboard_auth.locked_out(st.session_state)
        with st.form("login"):
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", disabled=wait > 0)
        if wait > 0:
            st.warning(f"Too many wrong attempts. Try again in {wait:.0f} s.")
        elif submitted:
            if dashboard_auth.attempt_login(password, st.session_state):
                dashboard_auth.touch(st.session_state)
                st.rerun()
            st.error("That password does not match. Try again, or reset it with `uv run aecom dashboard reset-password` on the machine.")
        st.stop()
    from aecom.config import get_settings

    if dashboard_auth.idle_expired(st.session_state, get_settings().dashboard_idle_hours):
        st.session_state["authenticated"] = False
        st.session_state["_stepup_until"] = 0.0
        st.rerun()
    dashboard_auth.touch(st.session_state)
    if first and st.sidebar.button("Log out", icon=":material/logout:", key="logout"):
        st.session_state["authenticated"] = False
        st.session_state["_stepup_until"] = 0.0
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


# --------------------------------------------------------------------------- layout helpers (shared by every page)
GLOSSARY = {
    "Tier (T0 to T4)": "How much an action matters. T0 only reads, T1 and T2 are small and logged, T3 needs your "
                       "approval, T4 is never allowed for an agent.",
    "Approval": "A request from an agent to do something risky. Nothing happens until you approve it.",
    "Kill switch": "Stops every action that could spend money or contact a customer. Deliveries of paid orders continue.",
    "Safe mode": "Set automatically when something serious is wrong. Only you can turn it off.",
    "Step-up": "Asking for your dashboard password again before a sensitive change, such as saving a key.",
    "Dead letter": "An internal message that could not be processed after several tries. Retry it after fixing the cause.",
}


def page_header(title: str, icon: str, subtitle: str | None = None) -> None:
    """Page title with a Material icon (name without colons, e.g. "dashboard"), an optional caption and a small
    popover that explains the terms used across the dashboard."""
    st.title(f":material/{icon}: {title}")
    if subtitle:
        st.caption(subtitle)
    with st.popover("What do these terms mean?", icon=":material/help:"):
        for term, meaning in GLOSSARY.items():
            st.markdown(f"**{term}**: {meaning}")


def kpi_row(items: Iterable[Kpi | tuple]) -> None:
    """A wrapping row of bordered metrics. Items: Kpi(label, value, delta=None, help=None) or plain tuples."""
    row = st.container(horizontal=True)
    for item in items:
        k = item if isinstance(item, Kpi) else Kpi(*item)
        row.metric(k.label, k.value, k.delta, help=k.help, border=True)


def empty_state(icon: str, title: str, hint: str = "") -> None:
    """A bordered 'nothing here yet' box instead of an empty table."""
    with st.container(border=True):
        st.markdown(f"#### :material/{icon}: {title}")
        if hint:
            st.caption(hint)


_BADGE = {"ok": "green", "approved": "green", "complete": "green", "delivered": "green", "resolved": "green", "active": "green",
          "pending": "orange", "running": "orange", "queued": "orange", "warn": "orange", "open": "orange", "info": "blue",
          "rejected": "red", "error": "red", "fail": "red", "failed": "red", "expired": "gray", "disabled": "gray"}


def status_badge(status: str, label: str | None = None) -> None:
    st.badge(label or status, color=_BADGE.get(str(status).lower(), "gray"))


def reviewer_name() -> str:
    """The owner's name for audit trails. One sidebar input (rendered by the navigation) shared by every page."""
    return str(st.session_state.get("reviewer_name", "")).strip()


def agent_spec(agent_id: str) -> dict[str, str]:
    from aecom.persona.loader import load_spec

    try:
        ident = load_spec(agent_id).identity
    except Exception:  # noqa: BLE001 - an unknown id must never break a page
        ident = {}
    return {"name": str(ident.get("display_name") or agent_id.replace("_", " ").title()),
            "emoji": str(ident.get("emoji") or ""), "color": str(ident.get("color") or "")}


def agent_avatar(agent_id: str) -> str:
    return agent_spec(agent_id)["emoji"] or ":material/smart_toy:"


def agent_label(agent_id: str) -> str:
    spec = agent_spec(agent_id)
    return f"{spec['emoji']} {spec['name']}".strip()


def confirm_dialog(title: str, body: str, on_confirm: Callable[[], None], *, confirm_label: str = "Confirm",
                   type_to_confirm: str | None = None) -> None:
    """Open a modal. Call it from `if st.button(...):`. `on_confirm` runs only after the owner confirms (and, for
    dangerous actions, typed ``type_to_confirm``)."""

    @st.dialog(title)
    def _dialog() -> None:
        st.markdown(body)
        ok = True
        if type_to_confirm:
            ok = st.text_input(f"Type {type_to_confirm} to confirm") == type_to_confirm
        row = st.container(horizontal=True)
        if row.button(confirm_label, type="primary", disabled=not ok):
            on_confirm()
            st.rerun()
        if row.button("Cancel"):
            st.rerun()

    _dialog()


NEED_NAME = ("Enter your name in the sidebar first (on a phone, tap the \u00bb at the top left). It is recorded in the audit log."
             "It is recorded in the audit log.")


def need_name(action: str = "") -> str | None:
    """The reviewer's name, or None after showing one consistent hint. Call before an action that is audited."""
    name = reviewer_name()
    if not name:
        st.warning(NEED_NAME if not action else f"{NEED_NAME} Needed to {action}.")
    return name or None


def field_error(message: str) -> None:
    """One line directly under a field, for live validation (use fields outside st.form to get it on each change)."""
    st.caption(f":red[:material/error: {message}]")


def field_ok(message: str) -> None:
    st.caption(f":green[:material/check_circle: {message}]")


def validated_text_input(label: str, *, key: str, validate: Callable[[str], str | None], success: str | None = None,
                         **kwargs: Any) -> tuple[str, bool]:
    """A text input with live feedback. ``validate`` returns an error message or None. Returns (value, is_valid);
    an empty value is not valid but shows no error until the owner has typed something."""
    value = st.text_input(label, key=key, **kwargs)
    error = validate(value) if value else None
    if error:
        field_error(error)
    elif value and success:
        field_ok(success)
    return value, bool(value) and error is None


STEP_UP_SECONDS = 300


def step_up(reason: str, key: str = "stepup") -> bool:
    """True when the owner re-entered the dashboard password in the last 5 minutes. Otherwise renders a password box
    and returns False. Critical settings (secrets, the login itself, policy loosening) require this."""
    import time

    if dashboard_auth.disabled() or not dashboard_auth.is_configured():
        st.warning("Turn on the dashboard login (Settings > Security) before changing this.")
        return False
    if float(st.session_state.get("_stepup_until", 0.0)) > time.time():
        return True
    with st.form(f"{key}_form"):
        st.caption(reason)
        password = st.text_input("Dashboard password", type="password", key=f"{key}_pw")
        go = st.form_submit_button("Confirm password")
    wait = dashboard_auth.locked_out(st.session_state)
    if wait > 0:
        st.warning(f"Too many wrong attempts. Try again in {wait:.0f} s.")
    elif go:
        if dashboard_auth.confirm_password(password, st.session_state):
            st.session_state["_stepup_until"] = time.time() + STEP_UP_SECONDS
            st.rerun()
        st.error("That password does not match. Try again, or reset it with `uv run aecom dashboard reset-password` on the machine.")
    return False


def refresh_settings() -> None:
    """Pick up settings another process changed (config_version); cheap, call once per run from the entrypoint."""
    from aecom import settings_registry

    settings_registry.refresh_if_changed()


# column_config shortcuts, so tables look the same on every page
def money(label: str) -> Any:
    return st.column_config.NumberColumn(label, format="€%.2f")


def when(label: str) -> Any:
    return st.column_config.DatetimeColumn(label, format="YYYY-MM-DD HH:mm")


def link(label: str, pattern: str) -> Any:
    """LinkColumn; ``pattern`` is a regex whose first group is shown as text, e.g. r".*run_id=(.*)"."""
    return st.column_config.LinkColumn(label, display_text=pattern)
