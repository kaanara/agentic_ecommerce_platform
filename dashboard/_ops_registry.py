# Govern > Operations > Tool grants tab: per-agent allowlists (`aecom registry list`/`revoke`).
#
# `render_grants_tab(by)` is imported and called by operations.py (not defined there). Only
# `aecom.registry.tools` touches `Allowlist` rows here (list_allowlists/revoke).

from __future__ import annotations

import pandas as pd
import streamlit as st

from _common import agent_label, confirm_dialog, empty_state
from aecom.registry.tools import list_allowlists, revoke


def _do_revoke(agent_id: str, tool_name: str, by: str, note: str) -> None:
    res = revoke(agent_id, tool_name, by, note)
    if res["removed"]:
        st.success(f"Revoked {tool_name} from {agent_id}.")
    else:
        st.info(f"{agent_id} had no grant for {tool_name}; nothing to revoke.")
    if res.get("warning"):
        st.warning(res["warning"])
    st.rerun()


def render_grants_tab(by: str) -> None:
    st.caption("Revoking a tool is human-only: no agent tool can reach this. `aecom init` (every CLI command runs "
              "it) additively re-adds a tool still listed in the agent's TOOLS.md, so remove it there too to keep "
              "it revoked.")
    allowlists = list_allowlists()
    if not allowlists:
        empty_state("key_off", "No tool grants yet", "Run `aecom init` to seed the registry.")
        return

    agent_ids = sorted(allowlists)
    options = {agent_label(a): a for a in agent_ids}
    chosen_label = st.selectbox("Agent", list(options), key="_grants_agent")
    agent_id = options[chosen_label]
    tools = sorted(allowlists.get(agent_id, []))

    if not tools:
        empty_state("key_off", "No tools granted to this agent")
        return
    st.dataframe(pd.DataFrame({"tool": tools}), hide_index=True, width="stretch")

    st.markdown("**Revoke a tool**")
    tool_name = st.selectbox("Tool", tools, key=f"_grants_tool_{agent_id}")
    note = st.text_input("Note (optional)", key=f"_grants_note_{agent_id}")
    if st.button("Revoke", key=f"_grants_revoke_{agent_id}", icon=":material/block:"):
        if not by:
            st.warning("Enter your name in the sidebar first (on a phone, tap the \u00bb at the top left). It is recorded in the audit log.")
        else:
            confirm_dialog(f"Revoke {tool_name} from {agent_id}?",
                           "The agent loses this tool immediately. If it is still listed in the agent's TOOLS.md, "
                           "the next `aecom init` re-adds it.", lambda: _do_revoke(agent_id, tool_name, by, note),
                           confirm_label="Revoke")
