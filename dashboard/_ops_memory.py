# Govern > Operations > Memory review tab (I15): shared agent memories waiting for a human.
#
# `render_memory_tab(by)` is imported and called by operations.py (not defined there). Only
# `aecom.memory_review` touches `Memory` rows here (list_pending/approve/reject); content is untrusted
# agent-written text, so it is always masked (I16) and rendered as plain text, never as markdown/HTML.

from __future__ import annotations

import streamlit as st

from _common import empty_state
from aecom import memory_review
from aecom.observability import mask


def _do_review(action: str, memory_id: str, by: str, note: str) -> None:
    fn = memory_review.approve if action == "approve" else memory_review.reject
    try:
        fn(memory_id, by, note)
    except ValueError as exc:  # already decided elsewhere, or an unknown id
        st.error(str(exc))
        return
    st.success(f"{'Approved' if action == 'approve' else 'Rejected'} {memory_id}.")
    st.rerun()


def _card(m: dict, by: str) -> None:
    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"**{m['namespace']}** \u00b7 written by {m['source_agent'] or 'unknown'}")
        top[1].caption(f"run {m['source_run_id'] or '-'}")
        top[2].caption(f"{m['created_at']:%Y-%m-%d %H:%M}")
        st.badge("tainted: this run read untrusted content" if m["tainted"] else "not tainted",
                 color="red" if m["tainted"] else "green")
        st.text(mask(m["content"]))
        note = st.text_input("Note (optional)", key=f"_memory_note_{m['id']}")
        row = st.container(horizontal=True)
        if row.button("Approve", key=f"_memory_approve_{m['id']}", type="primary", icon=":material/check:"):
            _do_review("approve", m["id"], by, note)
        if row.button("Reject", key=f"_memory_reject_{m['id']}", icon=":material/close:"):
            _do_review("reject", m["id"], by, note)


def render_memory_tab(by: str) -> None:
    st.caption("Shared memories an agent wrote that no human has reviewed yet. Only **approved** shared memories "
              "are recalled by other agents; rejecting keeps the row for the record but it is never recalled.")
    if not by:
        st.warning("Enter your name in the sidebar first (on a phone, tap the \u00bb at the top left). It is recorded in the audit log.")
        return
    pending = memory_review.list_pending()
    if not pending:
        empty_state("task_alt", "Nothing waiting for review", "New shared memories appear here as agents write them.")
        return
    for m in pending:
        _card(m, by)
