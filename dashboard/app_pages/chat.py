# Chat with the agents: conversations, streaming replies and tool steps.
# The run itself lives in aecom.chat.converse - the single entry point shared with the API and the messaging
# channels. This page only drives it: it lists/searches conversations, renders history from aecom.conversations,
# and bridges converse's on_event callback to st.write_stream through a thread + queue (converse runs
# synchronously and calls on_event from a worker thread).
from __future__ import annotations

import base64
import queue
import threading
from typing import Any

import streamlit as st

from _common import (
    agent_avatar,
    agent_label,
    empty_state,
    page_header,
    require_login,
    reviewer_name,
)
from aecom import chat, conversations

require_login()
page_header("Chat", "forum", "Talk to the agents. Replies stream in, with a step for every tool call.")

blocked = chat.blocked_reason()
if blocked is not None:
    empty_state("lock", "Turn on login", blocked)
    st.stop()

AGENTS = chat.chat_agents() or ["ceo"]
STARTERS: dict[str, list[str]] = {
    "ceo": ["What needs my attention today?", "Summarize this week"],
    "finance": ["How's our cash position?", "Any budget overruns?"],
    "researcher": ["What are competitors doing?", "Find a new product idea"],
    "sentinel": ["Any open incidents?", "Is everything healthy?"],
    "foreman": ["What's blocked right now?", "Status of today's runs"],
}
FILTERS = ["Inbox", "All", "Archived"]


def _build_attachment(file: Any) -> dict[str, Any]:
    """One ``converse(attachments=...)`` entry from an uploaded ``st.chat_input`` file (<=5 MB, enforced by the
    widget). Text and CSV are stored as text; images and PDFs are base64 so they stay JSON-serializable."""
    mime = file.type or ""
    name = file.name or "file"
    if mime.startswith("image/"):
        return {"type": "image", "name": name, "content": base64.b64encode(file.getvalue()).decode("ascii")}
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return {"type": "pdf", "name": name, "content": base64.b64encode(file.getvalue()).decode("ascii")}
    if mime == "text/csv" or name.lower().endswith(".csv"):
        return {"type": "csv", "name": name, "content": file.getvalue().decode("utf-8", "replace")}
    return {"type": "text", "name": name, "content": file.getvalue().decode("utf-8", "replace")}


def _show_tool_event(event: dict[str, Any], statuses: dict[str, Any]) -> None:
    """Render one ``tool_start``/``tool_end``/``escalation`` event as (or into) an ``st.status`` step."""
    etype = event.get("type")
    key = event.get("tool_call_id") or event.get("name") or etype
    if etype == "tool_start":
        statuses[key] = st.status(f"Running {event.get('name', 'tool')}…", state="running")
    elif etype == "tool_end":
        box = statuses.pop(key, None) or st.status(str(event.get("name", "tool")))
        box.update(label=f"{event.get('name', 'tool')} done", state="complete")
        if event.get("result") is not None:
            box.write(event["result"])
    elif etype == "escalation":
        st.warning("Needs your approval - see [Approvals](/approvals).", icon=":material/fact_check:")


def _stream_turn(conversation_id: str, text: str, attachments: list[dict[str, Any]]):
    """Run ``chat.converse`` in a background thread and stream its ``text_delta`` events through
    ``st.write_stream``; tool/escalation events render as a side effect while streaming. Falls back to a plain
    error message (no crash) if ``converse`` raises, so the page survives a stub or a broken run."""
    events: queue.Queue = queue.Queue()
    outcome: dict[str, Any] = {}
    statuses: dict[str, Any] = {}

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "text_delta":
            events.put(("text", event.get("text", "")))
        else:
            events.put(("event", event))

    def worker() -> None:
        try:
            outcome["turn"] = chat.converse(conversation_id, text, attachments=attachments, on_event=on_event)
        except Exception as exc:  # noqa: BLE001 - surfaced to the owner, must never crash the page
            outcome["error"] = exc
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def _chunks():
        while (item := events.get()) is not None:
            kind, payload = item
            if kind == "text":
                yield payload
            else:
                _show_tool_event(payload, statuses)

    st.write_stream(_chunks)
    thread.join()
    if "error" in outcome:
        st.error(f"The reply failed: {outcome['error']}")
        return None
    return outcome["turn"]


def _render_list(convos: list[dict[str, Any]], selected: str | None) -> None:
    if not convos:
        st.caption("No conversations here yet.")
    for c in convos:
        label = f"{agent_avatar(c['agent_id'])} {c['title'] or 'New chat'}"
        if c["unread"]:
            label += f" ({c['unread']})"
        if st.button(label, key=f"conv_{c['id']}", type="primary" if c["id"] == selected else "secondary"):
            st.query_params["conversation"] = c["id"]
            conversations.mark_read(c["id"])
            st.rerun()


def _new_chat_controls() -> None:
    agent_id = st.pills("Agent", AGENTS, format_func=agent_label, default=AGENTS[0], key="new_chat_agent")
    starter = None
    chips = STARTERS.get(agent_id, [])
    if chips:
        row = st.container(horizontal=True)
        for chip in chips:
            if row.button(chip, key=f"starter_{agent_id}_{chip}"):
                starter = chip
    if st.button("New chat", icon=":material/add_comment:", type="primary") or starter:
        cid = conversations.create(agent_id, owner=reviewer_name())
        st.query_params["conversation"] = cid
        if starter:
            st.session_state["_chat_pending"] = starter
        st.rerun()


# --------------------------------------------------------------------------- conversation picker (a popover, so it
# stays out of the way on narrow screens as well as wide ones)
with st.popover("Conversations", icon=":material/forum:"):
    _new_chat_controls()
    st.divider()
    search = st.text_input("Search", key="chat_search", placeholder="Search by title")
    filter_name = st.segmented_control("Filter", FILTERS, default="Inbox", key="chat_filter") or "Inbox"
    convos = conversations.list_conversations(include_archived=filter_name == "Archived",
                                              only_unread=filter_name == "Inbox", search=search or "")
    _render_list(convos, st.query_params.get("conversation"))

selected = st.query_params.get("conversation")
convo = conversations.get(selected) if selected else None
if convo is None:
    if selected:
        del st.query_params["conversation"]
    empty_state("forum", "No conversation selected", "Pick one from Conversations, or start a new chat.")
    st.stop()

top = st.container(horizontal=True)
top.subheader(f"{agent_label(convo['agent_id'])} · {convo['title'] or 'New chat'}")
if top.button("Unarchive" if convo["archived"] else "Archive", icon=":material/archive:"):
    conversations.archive(convo["id"], not convo["archived"])
    st.rerun()

for m in convo["messages"]:
    avatar = agent_avatar(convo["agent_id"]) if m["role"] == "assistant" else ":material/person:"
    with st.chat_message(m["role"], avatar=avatar):
        st.markdown(m["content"])
        for step in m.get("tool_steps") or []:
            name = step.get("name") or step.get("tool_name") or "tool"
            with st.status(str(name), state="complete"):
                st.json(step, expanded=False)
        if m.get("escalations"):
            st.caption(":material/fact_check: Waiting on your approval - see [Approvals](/approvals).")
        if m["role"] == "assistant":
            cost = f"€{m['cost_eur']:.4f}" if m.get("cost_eur") else "no cost"
            run = f" · [Run detail](/run?run_id={m['agent_run_id']})" if m.get("agent_run_id") else ""
            st.caption(f"{cost}{run}")

pending = st.session_state.pop("_chat_pending", None)
submitted = st.chat_input("Message", accept_file=True, file_type=["txt", "csv", "pdf", "png", "jpg", "jpeg"],
                          max_upload_size=5, submit_mode="disable", key=f"chat_input_{convo['id']}")
text = pending or (submitted.text if submitted else None)
attachments = [_build_attachment(f) for f in submitted.files] if submitted and submitted.files else []
if text:
    with st.chat_message("user", avatar=":material/person:"):
        st.markdown(text)
    with st.chat_message("assistant", avatar=agent_avatar(convo["agent_id"])):
        turn = _stream_turn(convo["id"], text, attachments)
    if turn is not None and turn.status != "ok":
        st.error(f"Reply status: {turn.status}")
    st.rerun()
