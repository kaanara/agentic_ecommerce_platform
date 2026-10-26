import json

import streamlit as st

from _common import sql
from aecom import approvals
from aecom.bus.events import drain

st.set_page_config(page_title="Approvals", page_icon="✅", layout="wide")
st.title("✅ Human-in-the-loop approvals")
st.caption("When you approve, the agent's call runs **exactly** as shown (including any args you edit). "
           "Rejecting it notifies the agent through the event bus.")

TIER_BADGE = {"T2": "🟠 T2 bounded risk", "T3": "🔴 T3 high stakes"}
reviewer = st.text_input("Reviewer name", value=st.session_state.get("reviewer", ""), placeholder="your name")
st.session_state["reviewer"] = reviewer

pending = approvals.list_approvals("pending")
st.metric("Pending", len(pending))
if not pending:
    st.success("Nothing waiting for you.")

for a in pending:
    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"### `{a['tool_name']}` · {a['agent_id']}")
        top[1].markdown(TIER_BADGE.get(a["risk_tier"], a["risk_tier"]))
        top[2].caption(f"{a['created_at']:%Y-%m-%d %H:%M} · {a['id']}")
        st.markdown(f"**Why it needs you:** {a['reason']}")
        args_text = st.text_area("Arguments (editable JSON)", json.dumps(a["args"], indent=2, ensure_ascii=False),
                                 key=f"args_{a['id']}", height=160)
        if a["context"]:
            with st.expander("Task context"):
                st.text(a["context"])
        note = st.text_input("Note", key=f"note_{a['id']}")
        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Approve & execute", key=f"ok_{a['id']}", type="primary", disabled=not reviewer):
            try:
                edited = json.loads(args_text)
            except json.JSONDecodeError as exc:
                st.error(f"Invalid JSON: {exc}")
                st.stop()
            override = {k: v for k, v in edited.items() if a["args"].get(k) != v} or None
            try:
                res = approvals.approve(a["id"], reviewer, note, override)
            except ValueError as exc:  # kill switch on, or already decided elsewhere
                st.error(str(exc))
                st.stop()
            drain()
            (st.success if res["status"] == "executed" else st.error)(f"{res['status']}: {res['result'][:300]}")
            st.rerun()
        if b2.button("Reject", key=f"no_{a['id']}", disabled=not reviewer):
            approvals.reject(a["id"], reviewer, note)
            drain()
            st.rerun()

st.divider()
st.subheader("History")
st.dataframe(sql("""SELECT decided_at, status, risk_tier, agent_id, tool_name, decided_by, decision_note, substr(result,1,200) AS result
                    FROM ops_approvals WHERE status <> 'pending' ORDER BY decided_at DESC LIMIT 100"""),
             hide_index=True, width="stretch")
