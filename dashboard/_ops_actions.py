# Govern > Operations > "Run and maintenance" tab: the CLI-only actions (aecom run, store sync, reconcile, monitors
# run, agentmail poll) as buttons. A partial module - operations.py already calls require_login() and passes `by`;
# this file must not call it again.
#
# Heavy work never runs here: every action only queues a row in ops_jobs via aecom.ops_actions.request(); the
# scheduler's jobs.run_pending() tick (every ~30 s) runs the handler and writes the result back, which the table at
# the bottom of this tab reads. Same pattern as _settings_backups.py's "Back up now" / "Verify".

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from _common import mask_df
from aecom import jobs, ops_actions
from aecom.config import get_settings

OPS_KINDS = ("run", "sync_store", "reconcile", "monitors", "agentmail_poll")


def _queue(kind: str, by: str, **params: Any) -> None:
    if not by:
        st.warning("Enter your name in the sidebar first (on a phone, tap the \u00bb at the top left). It is recorded in the audit log.")
        return
    try:
        ops_actions.request(kind, by, **params)
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.success("Queued. The scheduler runs it within its next tick (about 30 s); refresh this tab to see the result.")


def _jobs_table() -> pd.DataFrame:
    """The last 15 jobs across every kind this tab queues, newest first, with a masked/truncated result column
    (same style as run_detail.py's timeline: a short result string through to the secret-masking helper)."""
    rows: list[dict[str, Any]] = []
    for kind in OPS_KINDS:
        rows.extend(jobs.list_jobs(kind, limit=15))
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    out = []
    for r in rows[:15]:
        result = json.dumps(r["result"], default=str, ensure_ascii=False)[:300] if r["result"] else ""
        out.append({"when": str(r["created_at"])[:19], "kind": r["kind"], "status": r["status"],
                    "requested by": r["requested_by"], "result": result})
    return mask_df(pd.DataFrame(out, columns=["when", "kind", "status", "requested by", "result"]), ["result"])


def render_actions_tab(by: str) -> None:
    settings = get_settings()
    if settings.llm_mode == "scripted":
        st.caption(f"LLM_MODE={settings.llm_mode}: a company run here answers from the scripted test model, not a "
                  "real LLM - safe to try.")
    else:
        st.warning(f"LLM_MODE={settings.llm_mode}: a company run here calls the real LLM and spends real money "
                  "(subject to each agent's daily budget).", icon=":material/payments:")

    st.subheader("Company run")
    directive = st.text_area("Directive", key="_ops_run_directive",
                             placeholder="e.g. Launch a weekend flash sale on the best-selling SKU",
                             help=f"Up to {ops_actions.MAX_DIRECTIVE_LEN} characters.")
    if st.button("Start company run", icon=":material/play_arrow:", key="_ops_run_btn"):
        _queue("run", by, directive=directive)

    st.divider()
    st.subheader("Maintenance")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Sync store catalog", icon=":material/sync:", key="_ops_sync_btn"):
        _queue("sync_store", by)

    older_than = c2.number_input("Reconcile: older than (minutes)", min_value=1, value=15, step=1,
                                 key="_ops_reconcile_minutes")
    if c2.button("Reconcile now", icon=":material/restart_alt:", key="_ops_reconcile_btn"):
        _queue("reconcile", by, older_than_minutes=int(older_than))

    if c3.button("Run monitors now", icon=":material/monitor_heart:", key="_ops_monitors_btn"):
        _queue("monitors", by)

    if c4.button("Poll inbox now", icon=":material/mail:", key="_ops_poll_btn"):
        _queue("agentmail_poll", by)

    st.divider()
    st.subheader("Recent actions")
    table = _jobs_table()
    if table.empty:
        st.caption("No actions queued yet.")
    else:
        st.dataframe(table, hide_index=True, width="stretch")
    st.caption("The scheduler runs queued actions within about 30 seconds; refresh this tab to see results.")
