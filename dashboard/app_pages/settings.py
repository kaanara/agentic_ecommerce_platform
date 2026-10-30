# Govern > Settings: every option the owner can change, generated from the registry (§5b).
#
# Only `aecom.settings_registry` public functions touch stored values here (never ops_settings/.env directly), and
# Security uses only `aecom.dashboard_auth` service functions. Secrets are never rendered: only set/not set, the last
# 4 characters and a write-only input. Policy, Agents and Backups tabs are rendered by the `_settings_*` partials.
# A secret with an entry in `aecom.integrations.ping.PINGS` also gets a "Test" button (shown only once it is set)
# that makes one real, read-only call and reports back with `st.toast` - the secret value never appears in it.

import pandas as pd
import streamlit as st
import yaml

from _settings_agents import render_agents_tab
from _settings_backups import render_backups_tab
from _settings_policy import render_policy_tab
from _common import confirm_dialog, field_error, page_header, require_login, reviewer_name, step_up, validated_text_input
from aecom import dashboard_auth as auth
from aecom import settings_registry as reg
from aecom.integrations.ping import PINGS

require_login()
page_header("Settings", "settings",
            "Every option lives here. Search, or open a tab. Most changes apply live; a few need a restart.")

# Fields grouped by section, in registry declaration order.
_SECTION_FIELDS: dict[str, list[str]] = {}
for _key, _field in reg.FIELDS.items():
    _SECTION_FIELDS.setdefault(_field.section, []).append(_key)

PLACEHOLDER_TABS: dict[str, str] = {}

# N4: the (former 15) top-level tabs, grouped into 5 with the sections nested inside as `st.tabs` - see
# `aecom.settings_registry.GROUPS` for the grouping (shared with the tests, so both stay in step).
GROUP_NAMES: list[str] = list(reg.GROUPS.keys())

# Never-blocking format hints for write-only secrets, shown under the input (N6).
SECRET_HINTS: dict[str, str] = {
    "anthropic_api_key": "Anthropic keys start with sk-ant-.",
    "telegram_bot_token": "Telegram bot tokens look like 123456:ABC...",
}


# --------------------------------------------------------------------------- generic field rendering
def _widget(field: reg.Field, current: object, key: str):
    if field.kind == "bool":
        return st.toggle(field.label, value=bool(current), key=key)
    if field.kind == "int":
        return st.number_input(field.label, value=int(current) if current is not None else 0, step=1,
                               min_value=int(field.min) if field.min is not None else None,
                               max_value=int(field.max) if field.max is not None else None, key=key)
    if field.kind == "float":
        return st.number_input(field.label, value=float(current) if current is not None else 0.0, step=0.1,
                               min_value=float(field.min) if field.min is not None else None,
                               max_value=float(field.max) if field.max is not None else None, key=key)
    if field.kind == "enum":
        choices = list(field.choices)
        index = choices.index(current) if current in choices else 0
        return st.selectbox(field.label, choices, index=index, key=key)
    if field.kind == "list":
        return st.text_area(field.label, value=", ".join(current) if current else "", key=key,
                            help="Comma or newline separated.")
    if field.kind == "pair":
        return st.text_input(field.label, value=(f"{current[0]:g}, {current[1]:g}" if current else ""), key=key,
                             help="Two numbers: input, output.")
    return st.text_input(field.label, value="" if current is None else str(current), key=key)  # str, schedule, time


def _apply_set(key: str, raw: object, by: str) -> None:
    field = reg.FIELDS[key]
    try:
        reg.set_value(key, raw, by, step_up_ok=True)
    except (ValueError, reg.StepUpRequired) as exc:
        st.error(str(exc))
        return
    if field.apply == "restart":
        st.session_state.setdefault("_restart_needed", set()).add(key)
    st.success(f"Saved {field.label}.")
    st.rerun()


def _apply_reset(key: str, by: str) -> None:
    field = reg.FIELDS[key]
    try:
        reg.reset_value(key, by, step_up_ok=True)
    except (ValueError, reg.StepUpRequired) as exc:
        st.error(str(exc))
        return
    st.success(f"Reset {field.label} to its default.")
    st.rerun()


def _save(key: str, raw: object, by: str) -> None:
    if not by:
        st.warning("Enter your name in the sidebar before saving.")
        return
    if reg.needs_step_up(key):
        # The password prompt must be rendered on every rerun (not only this one) until confirmed, so the pending
        # action is parked in session_state; `_apply_pending_stepup`, called unconditionally near the top of the
        # page, is what actually asks for the password and applies the change once it is confirmed.
        st.session_state["_stepup_pending"] = {"kind": "set", "key": key, "raw": raw, "by": by}
        st.rerun()
        return
    _apply_set(key, raw, by)


def _reset(key: str, by: str) -> None:
    if not by:
        st.warning("Enter your name in the sidebar before resetting.")
        return
    if reg.needs_step_up(key):
        st.session_state["_stepup_pending"] = {"kind": "reset", "key": key, "by": by}
        st.rerun()
        return
    _apply_reset(key, by)


def _test_secret(key: str) -> None:
    """Run `key`'s ping (one real, read-only call) and report the result with a toast - never the secret value."""
    ok, message = PINGS[key]()
    st.toast(message, icon=":material/check_circle:" if ok else ":material/error:")


def _apply_pending_stepup() -> None:
    """Render the password prompt for a parked critical change, on every rerun, until it is confirmed or cancelled."""
    pending = st.session_state.get("_stepup_pending")
    if not pending:
        return
    field = reg.FIELDS[pending["key"]]
    verb = "reset" if pending["kind"] == "reset" else "change"
    with st.container(border=True):
        if not step_up(f"Confirm your password to {verb} {field.label}.", key=f"stepup_{pending['key']}"):
            if st.button("Cancel", key=f"stepup_cancel_{pending['key']}"):
                st.session_state.pop("_stepup_pending", None)
                st.rerun()
            return
    st.session_state.pop("_stepup_pending", None)
    if pending["kind"] == "set":
        _apply_set(pending["key"], pending["raw"], pending["by"])
    else:
        _apply_reset(pending["key"], pending["by"])


def _matches_search(field: reg.Field, key: str) -> bool:
    search = str(st.session_state.get("_settings_search", "")).strip().lower()
    if not search:
        return True
    return search in key or search in field.label.lower() or search in (field.help or "").lower()


def _render_field(key: str, by: str) -> bool:
    """Render one field's form (or a read-only/linked note). Returns False if hidden by the search box."""
    field = reg.FIELDS[key]
    if field.storage == "managed" or not _matches_search(field, key):
        return False
    if field.link:
        st.caption(f"**{field.label}** is managed on the **{field.link}** page." + (f" {field.help}" if field.help else ""))
        return True
    current = reg.value(key)
    source = reg.source(key)
    if field.storage == "readonly":
        st.text_input(field.label, value="" if current is None else str(current), disabled=True, key=f"ro_{key}",
                      help=field.help or None)
        st.caption(f"Read-only ({source}).")
        return True
    if field.kind == "secret":
        hint = reg.secret_hint(key)
        status = f"set (ends \u2026{hint['last4']})" if hint["set"] and hint["last4"] else ("set" if hint["set"] else "not set")
        if hint["set"] and key in PINGS:
            col_status, col_test = st.columns([5, 1])
            col_status.markdown(f"**{field.label}** \u2014 {status}")
            if col_test.button("Test", icon=":material/network_check:", key=f"test_{key}"):
                _test_secret(key)
        else:
            st.markdown(f"**{field.label}** \u2014 {status}")
        if field.help:
            st.caption(field.help)
        with st.form(f"secret_form_{key}", clear_on_submit=True):
            new_value = st.text_input("New value (write-only, never shown again)", type="password", key=f"secret_input_{key}")
            if key in SECRET_HINTS:
                field_error(SECRET_HINTS[key])
            submitted = st.form_submit_button("Save", type="primary")
        if submitted:
            if not new_value:
                st.warning("Enter a value first.")
            else:
                _save(key, new_value, by)
        st.divider()
        return True
    with st.form(f"form_{key}"):
        widget_value = _widget(field, current, f"input_{key}")
        caption = field.help or ""
        caption = f"{caption} Source: {source}." if caption else f"Source: {source}."
        if field.apply != "live":
            caption += f" Applies on {field.apply}."
        st.caption(caption)
        col_save, col_reset, _ = st.columns([1, 1, 4])
        save = col_save.form_submit_button("Save", type="primary")
        reset = col_reset.form_submit_button("Reset to default", disabled=(source == "default"))
    if save:
        _save(key, widget_value, by)
    if reset:
        _reset(key, by)
    st.divider()
    return True


def _section_tab(section: str, by: str) -> None:
    keys = _SECTION_FIELDS.get(section, [])
    rendered = False
    for key in keys:
        rendered = _render_field(key, by) or rendered
    if not rendered:
        st.caption("No settings match your search." if keys else "Nothing here yet.")


# --------------------------------------------------------------------------- Security tab (dashboard_auth only)
def _new_password_fields(new_key: str, repeat_key: str, repeat_label: str = "Repeat password") -> tuple[str, str, bool]:
    """New/repeat password inputs with live validation (N6) - kept outside any form so each keystroke updates the
    message below it. Returns (new, repeat, is_valid)."""
    new, new_ok = validated_text_input(
        "New password", key=new_key, type="password",
        validate=lambda v: None if len(v) >= auth.MIN_PASSWORD_LENGTH else
        f"Use at least {auth.MIN_PASSWORD_LENGTH} characters (you have {len(v)}).")
    repeat, repeat_ok = validated_text_input(
        repeat_label, key=repeat_key, type="password",
        validate=lambda v: None if v == new else "The two passwords are different. Type the same one twice.")
    return new, repeat, new_ok and repeat_ok


def _security_tab(by: str) -> None:
    st.caption("Only `dashboard_auth` writes the login mode and password hash; no agent tool can reach them.")
    if auth.disabled() or not auth.is_configured():
        st.info("Login is off or has no password: anyone who can reach this page can approve actions and use the "
                "kill switch. That's acceptable only because the dashboard listens on 127.0.0.1 behind an SSH "
                "tunnel. With login off, Chat is disabled and channel pairing is refused.", icon=":material/lock_open:")
        new, repeat, valid = _new_password_fields("set_pw_new", "set_pw_repeat")
        if st.button("Set password and turn login on", type="primary", disabled=not valid):
            if not by:
                st.warning("Enter your name in the sidebar first.")
            else:
                try:
                    auth.set_password(new, repeat, by, state=st.session_state)
                    st.session_state["authenticated"] = True
                    st.success("Password set. Login is now on.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
    else:
        st.success("Dashboard login is ON.", icon=":material/lock:")
        current = st.text_input("Current password", type="password", key="change_pw_current")
        new, repeat, valid = _new_password_fields("change_pw_new", "change_pw_repeat", "Repeat new password")
        if st.button("Change password", type="primary", disabled=not valid):
            if not by:
                st.warning("Enter your name in the sidebar first.")
            else:
                try:
                    auth.set_password(new, repeat, by, current=current, state=st.session_state)
                    st.success("Password changed.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        st.divider()
        st.markdown("**Turn login off**")
        st.caption("Needs the current password, plus a confirmation. Chat is disabled and pairing refused while off.")
        off_password = st.text_input("Current password", type="password", key="turn_off_pw")

        def _do_turn_off() -> None:
            try:
                auth.turn_login_off(by, off_password, state=st.session_state)
                st.success("Login turned off.")
            except ValueError as exc:
                st.error(str(exc))

        if st.button("Turn login off"):
            if not by:
                st.warning("Enter your name in the sidebar first.")
            else:
                confirm_dialog("Turn the dashboard login off?",
                               "Anyone who can reach the dashboard will then be able to approve actions and use the "
                               "kill switch. This is announced on every paired channel.", _do_turn_off,
                               confirm_label="Turn off")
    st.divider()
    st.markdown("**Idle timeout and API token**")
    _render_field("dashboard_idle_hours", by)
    _render_field("aecom_api_token", by)


# --------------------------------------------------------------------------- History / Export & Import tabs
def _history_tab() -> None:
    rows = reg.history(200)
    if not rows:
        st.caption("No settings changes yet.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _export_import_tab(by: str) -> None:
    st.markdown("**Export**")
    st.caption("Non-secret, non-critical settings that differ from their default, as YAML (to copy to another "
               "machine, e.g. the future cloud server). Secrets and critical settings are never exported.")
    data = reg.export_settings()
    yaml_text = yaml.safe_dump(data, sort_keys=True) if data else "# no overrides yet\n"
    st.download_button("Download settings.yaml", yaml_text, file_name="settings.yaml", mime="text/yaml")
    st.code(yaml_text, language="yaml")
    st.divider()
    st.markdown("**Import**")
    uploaded = st.file_uploader("settings.yaml", type=["yaml", "yml"])
    if uploaded is None:
        return
    try:
        parsed = yaml.safe_load(uploaded.getvalue().decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        st.error(f"Invalid YAML: {exc}")
        return
    if not isinstance(parsed, dict):
        st.error("The file must contain a mapping of key: value.")
        return
    if st.button("Apply import"):
        if not by:
            st.warning("Enter your name in the sidebar first.")
            return
        result = reg.import_settings(parsed, by)
        if result["applied"]:
            st.success(f"Applied: {', '.join(result['applied'])}")
        if result["skipped"]:
            st.info(f"Skipped (secret, critical or unknown): {', '.join(result['skipped'])}")
        if result["errors"]:
            st.error("; ".join(result["errors"]))
        st.rerun()


# --------------------------------------------------------------------------- page
def _render_tab(name: str, by: str) -> None:
    """Render one (nested, formerly top-level) tab by name - unchanged dispatch, now called from inside a group."""
    if name == "Security":
        _security_tab(by)
    elif name == "History":
        _history_tab()
    elif name == "Export / Import":
        _export_import_tab(by)
    elif name == "Policy":
        render_policy_tab(by)
    elif name == "Agents":
        render_agents_tab(by)
    elif name == "Backups and updates":
        _section_tab(name, by)
        render_backups_tab(by)
    elif name in PLACEHOLDER_TABS:
        st.caption(PLACEHOLDER_TABS[name])
    else:
        _section_tab(name, by)


by = reviewer_name()
if not by:
    st.info("Enter your name in the sidebar to save changes (browsing stays read-only until then).")

if st.session_state.get("_restart_needed"):
    labels = sorted(reg.FIELDS[k].label for k in st.session_state["_restart_needed"])
    st.warning("Restart needed to fully apply: " + ", ".join(labels) + ". Restart `aecom dashboard` (and the "
               "scheduler/API if running) when convenient.", icon=":material/restart_alt:")

_apply_pending_stepup()

st.text_input("Find a setting", key="_settings_search", placeholder="e.g. anthropic, backup, telegram, idle")

for group_tab, group_name in zip(st.tabs(GROUP_NAMES), GROUP_NAMES):
    with group_tab:
        names = reg.GROUPS[group_name]
        for tab, name in zip(st.tabs(names), names):
            with tab:
                _render_tab(name, by)

