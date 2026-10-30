# Govern > Settings > Policy tab (§5b): a rules table, a YAML editor, a diff, versions and rollback.
#
# `render_policy_tab(by)` is imported and called by settings.py (not defined there - see the module docstring for
# the 2-line patch). Only `aecom.policy.versions` touches `ops_policy_versions`; this module never writes the file.

from __future__ import annotations

import difflib

import pandas as pd
import streamlit as st
import yaml

from _common import NEED_NAME, field_error, field_ok, step_up
from aecom.config import get_settings
from aecom.policy import versions as pv


def _effective_yaml() -> str:
    return pv.active_yaml() or get_settings().policy_file.read_text()


def _diff(before: str, after: str) -> str:
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile="active",
                                          tofile="edited", lineterm=""))


def _rules_table(policy: dict) -> pd.DataFrame:
    rows = []
    for name, rule in sorted((policy.get("rules") or {}).items()):
        if not isinstance(rule, dict):
            continue
        limits = ", ".join(f"{k} \u2264 {v}" for k, v in (rule.get("limits") or {}).items())
        rows.append({"tool": name, "tier": rule.get("tier", ""), "agents": ", ".join(rule.get("agents") or []) or "any",
                     "limits": limits, "reason": rule.get("reason", "")})
    return pd.DataFrame(rows)


def _validation_status(yaml_text: str) -> tuple[bool, str]:
    """Live feedback for the YAML editor: a parse error with its line number, a content problem, or the green
    go-ahead - computed on every rerun (the editor lives outside any st.form), so Save can stay disabled until
    the text is actually valid."""
    try:
        yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            detail = getattr(exc, "problem", None) or str(exc).splitlines()[0]
            return False, f"Problem near line {mark.line + 1}: {detail}"
        return False, f"Invalid YAML: {exc}"
    try:
        pv.validate_yaml(yaml_text)
    except ValueError as exc:
        return False, str(exc)
    return True, "Valid, ready to save."


def _do_save(yaml_text: str, note: str, by: str, *, step_up_ok: bool) -> None:
    try:
        version_id = pv.save_version(yaml_text, note, by, step_up_ok=step_up_ok)
    except PermissionError:
        st.session_state["_policy_pending"] = {"kind": "save", "yaml": yaml_text, "note": note, "by": by}
        st.rerun()
        return
    except ValueError as exc:
        st.error(str(exc))
        return
    st.session_state.pop("_policy_pending", None)
    st.success(f"Saved and activated policy version #{version_id}.")
    st.rerun()


def _do_activate(version_id: int, by: str, *, step_up_ok: bool) -> None:
    try:
        pv.activate(version_id, by, step_up_ok=step_up_ok)
    except PermissionError:
        st.session_state["_policy_pending"] = {"kind": "activate", "version_id": version_id, "by": by}
        st.rerun()
        return
    except ValueError as exc:
        st.error(str(exc))
        return
    st.session_state.pop("_policy_pending", None)
    st.success(f"Activated policy version #{version_id}.")
    st.rerun()


def _apply_pending_stepup() -> None:
    """Render the password prompt for a parked loosening change, on every rerun, until confirmed or cancelled."""
    pending = st.session_state.get("_policy_pending")
    if not pending:
        return
    with st.container(border=True):
        st.warning("This loosens the policy (a lower tier, a higher limit, a dropped budget cap, or a T4 rule "
                  "change). Confirm your password to continue.", icon=":material/lock:")
        if not step_up("Confirm your password to apply this policy change.", key="policy_stepup"):
            if st.button("Cancel", key="policy_stepup_cancel"):
                st.session_state.pop("_policy_pending", None)
                st.rerun()
            return
    if pending["kind"] == "save":
        _do_save(pending["yaml"], pending["note"], pending["by"], step_up_ok=True)
    else:
        _do_activate(pending["version_id"], pending["by"], step_up_ok=True)


def render_policy_tab(by: str) -> None:
    st.caption("The rules `aecom.policy.gate` evaluates before every tool call (tier, agent restriction, limits, "
              "budgets). Saving creates a new version and activates it; any version can be rolled back to. A change "
              "that loosens an existing rule needs your dashboard password again.")

    current = _effective_yaml()
    try:
        policy = yaml.safe_load(current) or {}
    except yaml.YAMLError:
        policy = {}

    st.subheader("Rules")
    table = _rules_table(policy)
    if table.empty:
        st.caption("No rules parsed from the active policy.")
    else:
        st.dataframe(table, hide_index=True, width="stretch")

    st.subheader("Edit as YAML")
    edited = st.text_area("policy.yaml", value=current, height=400, key="_policy_yaml_edit")
    note = st.text_input("Note for this version", key="_policy_note", placeholder="why this change")
    if st.toggle("Show diff vs. the active version", key="_policy_show_diff"):
        st.code(_diff(current, edited) or "(no changes)", language="diff")
    valid, status = _validation_status(edited)
    if valid:
        field_ok(status)
    else:
        field_error(status)
    if st.button("Validate and save", key="_policy_save", icon=":material/save:", type="primary",
                disabled=not (by and valid)):
        _do_save(edited, note, by, step_up_ok=False)
    if not by:
        st.caption(NEED_NAME)
    # The step-up prompt for a parked loosening save/activate lives right here, next to the YAML editor and the
    # rollback picker below (not above the top-level tabs), so it appears where the owner is already looking.
    _apply_pending_stepup()

    st.divider()
    st.subheader("Versions")
    rows = pv.list_versions(50)
    if not rows:
        st.caption("No saved versions yet \u2014 the file in git is active.")
        return
    history = pd.DataFrame([{"id": r["id"], "active": "\u2705" if r["active"] else "", "note": r["note"],
                            "created_by": r["created_by"], "created_at": r["created_at"]} for r in rows])
    st.dataframe(history, hide_index=True, width="stretch")

    others = {f"#{r['id']} \u2014 {r['note'] or '(no note)'} ({r['created_by']})": r for r in rows if not r["active"]}
    if not others:
        st.caption("No other version to roll back to.")
        return
    choice = st.selectbox("Roll back to", list(others), key="_policy_rollback_choice")
    if st.button("Activate this version", key="_policy_rollback_btn", icon=":material/history:", disabled=not by):
        _do_activate(others[choice]["id"], by, step_up_ok=False)
    if not by:
        st.caption(NEED_NAME)
