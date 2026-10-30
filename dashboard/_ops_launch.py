# Govern > Operations > Launch tab (Roadmap L4): a read-only readiness checklist for the first real launch, plus
# progress toward the ~10 real sales that unlock autonomy promotion (see Trust and docs/wiki/Autonomy.md).
#
# `render_launch_tab(by)` is imported and called by operations.py (not defined there), the same contract every
# other `_ops_*` tab uses. Everything here reads `aecom.launch`; only the five human-only acknowledgements can be
# changed from this page, and only by a named human (`launch.acknowledge`, audited, no agent tool).

from __future__ import annotations

import streamlit as st

from _common import empty_state
from aecom import launch

STATUS_LABEL = {launch.TODO: "To do", launch.WARN: "Needs a look", launch.OK: "Ready"}
STATUS_ICON = {launch.TODO: "radio_button_unchecked", launch.WARN: "error", launch.OK: "check_circle"}
GROUP_ORDER = (launch.TODO, launch.WARN, launch.OK)


def _row(c: launch.Check, by: str) -> None:
    with st.container(border=True):
        st.markdown(f":material/{STATUS_ICON[c.status]}: **{c.title}**")
        st.caption(c.detail)
        if c.where_to_fix:
            st.caption(f"Where to fix: {c.where_to_fix}")
        if c.id.startswith("ack_") and c.status != launch.OK:
            key = c.id.removeprefix("ack_")
            if not by:
                st.caption("Enter your name in the sidebar first to acknowledge this.")
            elif st.button("I've done this", key=f"_launch_ack_{key}", icon=":material/task_alt:"):
                launch.acknowledge(key, by)
                st.success(f"Acknowledged: {c.title}. Refresh this tab to see it move to Ready.")


def render_launch_tab(by: str) -> None:
    st.caption("Roadmap L4: launch one product and get about 10 real sales, with you approving almost everything. "
              "Autonomy promotion stays locked until then (see the Trust page and the Autonomy doc). Every item "
              "below reads the real state of the company; only the acknowledgements can be ticked here, and only "
              "by a named human.")

    progress = launch.progress()
    st.progress(progress["fraction"],
                text=f"{progress['real_sales']} of {progress['threshold']} real sales "
                     f"(\u20ac{progress['revenue_eur']:.2f} net revenue)")
    st.caption(progress["stage_text"])

    rows = launch.checks()
    if launch.ready(rows):
        st.success("Nothing outstanding: every check is ready to launch.")

    grouped: dict[str, list[launch.Check]] = {s: [] for s in GROUP_ORDER}
    for c in rows:
        grouped.setdefault(c.status, []).append(c)

    for status in GROUP_ORDER:
        items = grouped[status]
        st.markdown(f"**{STATUS_LABEL[status]} ({len(items)})**")
        if not items:
            empty_state("check_circle" if status == launch.OK else "info", "Nothing here")
            continue
        for c in items:
            _row(c, by)
