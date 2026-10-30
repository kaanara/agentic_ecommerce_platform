# Govern > Settings > Agents tab (§5b): a markdown editor per persona (and company/) file, preview, diff and reset.
#
# `render_agents_tab(by)` is imported and called by settings.py (not defined there - see the module docstring for
# the 2-line patch). Only `aecom.persona.overrides` touches `ops_persona_overrides`; this module never edits a file
# tracked by git.

from __future__ import annotations

import difflib

import pandas as pd
import streamlit as st

from _common import agent_label, confirm_dialog
from aecom.persona import loader as pl
from aecom.persona import overrides as po

COMPANY_FILES = ("GOALS.md", "BRAND.md")


def _diff(before: str | None, after: str) -> str:
    return "\n".join(difflib.unified_diff((before or "").splitlines(), after.splitlines(), fromfile="tracked file",
                                          tofile="edit", lineterm=""))


def _scope_options() -> dict[str, str]:
    options = {"Company documents": "company"}
    for agent_id in pl.agent_ids():
        options[agent_label(agent_id)] = agent_id
    return options


def _files_for(scope: str) -> tuple[str, ...]:
    return COMPANY_FILES if scope == "company" else pl.REQUIRED_FILES


def _do_save(scope: str, file_name: str, content: str, by: str) -> None:
    try:
        po.save_override(scope, file_name, content, by)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(f"Saved {scope}/{file_name}.")
    st.rerun()


def _do_reset(scope: str, file_name: str, by: str) -> None:
    po.reset_override(scope, file_name, by)
    st.success(f"Reset {scope}/{file_name} to the tracked file.")
    st.rerun()


def render_agents_tab(by: str) -> None:
    st.caption("Persona and company/ document overrides. Files tracked by git are never edited here on the Pi - an "
              "edit is stored in `ops_persona_overrides` and the loader prefers it, so `scripts/update.sh`'s "
              "`git pull` never conflicts. A TOOLS.md change only adds tools to the allowlist (the existing, "
              "human-confirmed C5 resync); nothing is ever auto-revoked.")

    options = _scope_options()
    scope_label = st.selectbox("Scope", list(options), key="_agents_scope")
    scope = options[scope_label]
    files = _files_for(scope)
    file_name = st.selectbox("File", list(files), key=f"_agents_file_{scope}")

    tracked = po.file_text(scope, file_name)
    if tracked is None:
        st.info(f"{scope}/{file_name} does not exist in git yet \u2014 nothing to override.", icon=":material/info:")
        return

    detail = po.get_override_detail(scope, file_name)
    if detail:
        st.info(f"Overridden by {detail['updated_by']} on {detail['updated_at']:%Y-%m-%d %H:%M}.",
               icon=":material/edit_note:")
        if detail["stale"]:
            st.warning("The tracked file changed since this override was made (e.g. a `git pull`). Agents still use "
                      "your override; check the diff before resetting.", icon=":material/warning:")

    current = po.current_text(scope, file_name) or ""
    edit_key = f"_agents_edit_{scope}_{file_name}"
    edited = st.text_area(f"{scope}/{file_name}", value=current, height=400, key=edit_key)

    tab_preview, tab_diff = st.tabs(["Preview", "Diff vs. tracked file"])
    with tab_preview:
        st.markdown(edited)
    with tab_diff:
        st.code(_diff(tracked, edited) or "(no changes)", language="diff")

    col_save, col_reset = st.columns([1, 1])
    if col_save.button("Save override", key=f"_agents_save_{scope}_{file_name}", icon=":material/save:"):
        if not by:
            st.warning("Enter your name in the sidebar before saving.")
        elif file_name == po.TOOLS_FILE and scope != "company":
            confirm_dialog(f"Save {scope}/{file_name}?",
                           "Tools newly listed here are added to this agent's allowlist through the existing "
                           "additive resync. Nothing is ever auto-revoked; use `aecom registry revoke` for that.",
                           lambda: _do_save(scope, file_name, edited, by), confirm_label="Save")
        else:
            _do_save(scope, file_name, edited, by)
    if col_reset.button("Reset to file version", key=f"_agents_reset_{scope}_{file_name}",
                        disabled=detail is None, icon=":material/restart_alt:"):
        if not by:
            st.warning("Enter your name in the sidebar before resetting.")
        else:
            confirm_dialog(f"Reset {scope}/{file_name} to the tracked file?",
                           "Your override is deleted; the version from git applies again.",
                           lambda: _do_reset(scope, file_name, by), confirm_label="Reset")

    st.divider()
    st.subheader("All overrides")
    rows = po.list_overrides()
    if not rows:
        st.caption("No overrides yet.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
