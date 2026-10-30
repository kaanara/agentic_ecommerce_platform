import json

import streamlit as st

from _common import NEED_NAME, agent_label, empty_state, kpi_row, page_header, require_login, reviewer_name, sql
from aecom import approvals
from aecom.bus.events import drain

require_login()
page_header("Human-in-the-loop approvals", "fact_check",
            "When you approve, the agent's call runs **exactly** as shown (including any args you edit). "
            "Rejecting it notifies the agent through the event bus.")

TIER_BADGE = {"T2": "🟠 T2 bounded risk", "T3": "🔴 T3 high stakes"}


AMOUNT_KEYS = ("amount_eur", "budget_eur", "monthly_budget_eur", "price_eur", "new_price_eur", "total_eur", "amount")


def _summary(a: dict) -> str:
    """One plain line saying what approving does, with the amount first when the call carries one."""
    args = a.get("args") or {}
    for key in AMOUNT_KEYS:
        value = args.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"**Amount: \u20ac{value:,.2f}.** Approving runs `{a['tool_name']}` exactly as shown below."
    return f"Approving runs `{a['tool_name']}` with {len(args)} setting(s). Open the details to see them."


def _flash(kind: str, text: str) -> None:
    """Keep the result of Approve/Reject visible after the page reruns."""
    st.session_state["_approval_flash"] = (kind, text)


def _decide(reviewer: str, a: dict, args_text: str, note: str) -> None:
    """Approve the card: parse the (possibly edited) args, run it, and drain the event bus."""
    try:
        edited = json.loads(args_text)
    except json.JSONDecodeError as exc:
        st.error(f"The arguments are not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg}). "
                 "Fix them in the details below, or reset the text, then approve again.")
        return
    if not isinstance(edited, dict):
        st.error("The arguments must be a JSON object, like {\"amount_eur\": 50}.")
        return
    override = {k: v for k, v in edited.items() if a["args"].get(k) != v} or None
    try:
        res = approvals.approve(a["id"], reviewer, note, override)
    except ValueError as exc:  # kill switch on, or already decided elsewhere
        st.error(f"This could not be approved: {exc}")
        return
    drain()
    _flash("success" if res["status"] == "executed" else "error",
           f"{a['tool_name']}: {res['status']}. {res['result'][:300]}")
    st.rerun()


def _card(a: dict, reviewer: str) -> None:
    with st.container(border=True):
        top = st.columns([3, 1, 1])
        top[0].markdown(f"### `{a['tool_name']}` · {agent_label(a['agent_id'])}")
        top[1].markdown(TIER_BADGE.get(a["risk_tier"], a["risk_tier"]))
        top[2].caption(f"{a['created_at']:%Y-%m-%d %H:%M} · {a['id']}")
        st.markdown(f"**Why it needs you:** {a['reason']}")
        st.markdown(_summary(a))
        with st.expander("Details and editable arguments"):
            args_text = st.text_area("Arguments (JSON, you can edit them)", json.dumps(a["args"], indent=2, ensure_ascii=False),
                                     key=f"args_{a['id']}", height=160)
            if a["context"]:
                st.caption("Task context")
                st.text(a["context"])
        note = st.text_input("Note", key=f"note_{a['id']}")
        row = st.container(horizontal=True)
        if row.button("Approve & execute", key=f"ok_{a['id']}", type="primary", disabled=not reviewer):
            _decide(reviewer, a, args_text, note)
        if row.button("Reject", key=f"no_{a['id']}", disabled=not reviewer):
            approvals.reject(a["id"], reviewer, note)
            drain()
            _flash("info", f"{a['tool_name']}: rejected.")
            st.rerun()


def _pending_panel() -> None:
    """Refreshed every 10 s so a card another reviewer just decided (or a new one) shows up without a manual reload."""
    reviewer = reviewer_name()
    if flash := st.session_state.pop("_approval_flash", None):
        {"success": st.success, "error": st.error}.get(flash[0], st.info)(flash[1])
    if not reviewer:
        st.info(NEED_NAME)
    pending = approvals.list_approvals("pending")
    kpi_row([("Pending", len(pending))])
    if not pending:
        empty_state("task_alt", "Nothing waiting for you", "Approved and rejected calls land in History below.")
        return
    for a in pending:
        _card(a, reviewer)


if hasattr(st, "fragment"):
    st.fragment(run_every=10)(_pending_panel)()
else:  # older Streamlit without fragments: refresh by hand
    _pending_panel()
    st.button("Refresh", key="approvals_refresh")

st.divider()
st.subheader("History")
history = sql("""SELECT coalesce(decided_at, expires_at) AS decided_at, expires_at, status, risk_tier, agent_id,
                        tool_name, decided_by, decision_note, substr(result,1,200) AS result
                 FROM ops_approvals WHERE status <> 'pending' ORDER BY coalesce(decided_at, expires_at) DESC LIMIT 100""")
if history.empty:
    empty_state("history", "No decisions yet")
else:
    st.dataframe(history, hide_index=True, width="stretch")
