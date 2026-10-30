# Govern > Operations > Incident reports tab (K8/K10): incident -> GitHub issue markdown -> postmortem draft
# (`aecom incidents export ID [--live]` / `aecom incidents postmortem ID`).
#
# `render_incident_reports_tab(by)` is imported and called by operations.py (not defined there). Only
# `aecom.incidents` (listing) and `aecom.incident_export` (issue_body/export_issue/draft_postmortem) touch
# `Incident` rows here; both already mask and redact (I16) everything they return, so the preview is safe to show
# verbatim in a code block. The live GitHub call (`dry_run=False`) needs a configured token+repo, a fresh password
# step-up and the incident id typed out, because it posts a real issue to a possibly-public repo.

from __future__ import annotations

import streamlit as st

from _common import empty_state, step_up
from aecom import incident_export, incidents


def _live_configured() -> bool:
    """Mirrors export_issue's own check: a live issue needs GITHUB_REPO + GITHUB_TOKEN set."""
    from aecom.secrets import secrets as env_secrets

    return bool(env_secrets.get("GITHUB_REPO")) and bool(env_secrets.get("GITHUB_TOKEN"))


def _label(i: dict) -> str:
    title = f": {i['subject']}" if i["subject"] else ""
    return f"{i['id']} \u00b7 {i['severity']} \u00b7 {i['monitor']}{title}"


def _live_section(inc_id: str, by: str) -> None:
    if not _live_configured():
        return
    st.divider()
    st.markdown("**Create the GitHub issue (live)**")
    st.caption("Posts a real issue to the configured repo instead of only writing the local file. Needs your "
              "password again and the incident id typed out.")
    if not step_up("Confirm your password to create a live GitHub issue.", key=f"_inc_live_stepup_{inc_id}"):
        return
    typed = st.text_input("Type the incident id to confirm", key=f"_inc_live_typed_{inc_id}")
    if st.button("Create the GitHub issue", key=f"_inc_live_btn_{inc_id}", type="primary",
                disabled=typed != inc_id, icon=":material/bug_report:"):
        try:
            path = incident_export.export_issue(inc_id, dry_run=False)
        except ValueError as exc:
            st.error(str(exc))
            return
        st.success(f"GitHub issue created; local copy at {path}.")
        st.rerun()


def render_incident_reports_tab(by: str) -> None:
    st.caption("Issue markdown and postmortem drafts for incidents, already masked and redacted by the service. "
              "Nothing here deploys or merges; a human still opens the PR.")
    if not by:
        st.warning("Enter your name in the sidebar first (on a phone, tap the \u00bb at the top left). It is recorded in the audit log.")
        return

    include_resolved = st.checkbox("Include resolved incidents", value=True, key="_inc_include_resolved")
    rows = incidents.list_incidents()
    if not include_resolved:
        rows = [r for r in rows if r["status"] in incidents.ACTIVE]
    if not rows:
        empty_state("report", "No incidents to report on", "Incidents appear here as monitors raise them.")
        return

    options = {_label(r): r["id"] for r in rows}
    chosen = st.selectbox("Incident", list(options), key="_inc_select")
    inc_id = options[chosen]
    inc = next(r for r in rows if r["id"] == inc_id)

    cols = st.columns(3)
    cols[0].metric("Severity", inc["severity"])
    cols[1].metric("Status", inc["status"])
    cols[2].metric("Monitor", inc["monitor"])

    st.markdown("**Issue preview**")
    try:
        body = incident_export.issue_body(inc_id)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.code(body, language="markdown")

    row = st.container(horizontal=True)
    if row.button("Write issue file", key=f"_inc_export_{inc_id}", icon=":material/description:"):
        try:
            path = incident_export.export_issue(inc_id, dry_run=True)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success(f"Wrote {path}")
    if row.button("Draft postmortem", key=f"_inc_postmortem_{inc_id}", icon=":material/history_edu:"):
        try:
            rep = incident_export.draft_postmortem(inc_id, created_by=f"human:{by}")
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.success(f"Postmortem v{rep['version']} drafted for {inc_id}.")

    _live_section(inc_id, by)
