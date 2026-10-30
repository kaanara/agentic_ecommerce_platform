# Govern > Accounting > Decision review tab (J14): quarterly look back at accepted decisions (ADRs) - did each one
# deliver what it promised?
#
# `render_decision_review_tab(by)` is imported and called by accounting.py (not defined there). Only
# `aecom.decision_review` touches `company_decisions` / `ops_decision_reviews` here (due/evidence/record_review/
# list_reviews); no agent tool can write a review (see tools/catalog.py), so this tab is the only place one is
# recorded.

from __future__ import annotations

import pandas as pd
import streamlit as st

from _common import empty_state, when
from aecom import decision_review

_OUTCOME_LABELS = {"delivered": "Delivered", "partly": "Partly delivered", "not_delivered": "Not delivered",
                   "superseded": "Superseded"}


def _fact_line(facts: dict) -> str:
    parts = [f"{k.replace('_', ' ')}: {v}" for k, v in facts.items() if v is not None]
    return " \u00b7 ".join(parts)


def _save(decision_id: str, outcome: str, note: str, by: str) -> None:
    if not by:
        st.warning("Enter your name in the sidebar first (on a phone, tap the \u00bb at the top left). It is recorded in the audit log.")
        return
    try:
        decision_review.record_review(decision_id, outcome, note, by)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.success(f"Review saved for {decision_id}.")
    st.rerun()


def _card(d: dict, by: str) -> None:
    ev = decision_review.evidence(d["id"])
    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"**{d['title']}** \u00b7 {d['category']}")
        top[1].caption(f"decided by {d['decided_by']}")
        top[2].caption(f"{d['days_old']} days ago")
        st.caption(f"Chose: {d['choice']}")
        st.write(ev["rationale"])
        line = _fact_line(ev["facts"])
        if line:
            st.caption(line)
        elif ev.get("facts_note"):
            st.caption(ev["facts_note"])

        outcome = st.radio("Outcome", list(_OUTCOME_LABELS), format_func=lambda o: _OUTCOME_LABELS[o],
                           horizontal=True, key=f"_decrev_outcome_{d['id']}")
        note = st.text_area("Note", key=f"_decrev_note_{d['id']}", placeholder="What actually happened?")
        if outcome != "delivered" and not note.strip():
            st.caption(":red[A note is required for this outcome.]")
        if st.button("Save", key=f"_decrev_save_{d['id']}", type="primary", icon=":material/save:"):
            _save(d["id"], outcome, note, by)


def render_decision_review_tab(by: str) -> None:
    st.caption("Quarterly look back at accepted decisions: did each one deliver what it promised? A decision "
              "becomes due 60 days after being accepted and stays due until reviewed for the current quarter.")
    pending = decision_review.due()
    if not pending:
        empty_state("task_alt", "Nothing due this quarter",
                    "Accepted decisions appear here 60 days after being decided.")
    else:
        for d in pending:
            _card(d, by)

    st.divider()
    st.subheader("History")
    rows = decision_review.list_reviews()
    if not rows:
        st.caption("No reviews recorded yet.")
        return
    history = pd.DataFrame([{"decision_id": r["decision_id"], "quarter": r["quarter"],
                            "outcome": _OUTCOME_LABELS.get(r["outcome"], r["outcome"]), "note": r["note"],
                            "reviewed_by": r["reviewed_by"], "reviewed_at": r["reviewed_at"]} for r in rows])
    st.dataframe(history, hide_index=True, width="stretch", column_config={"reviewed_at": when("Reviewed at")})
