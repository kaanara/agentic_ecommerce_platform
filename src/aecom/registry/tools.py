"""Tool/skill/MCP registry: the source of truth for *what exists* and *who may use it*.

Every init registers built-ins and syncs allowlists *additively* from each agent's TOOLS.md: tools listed
there but missing from the DB are added (and audited). Rows are never removed, so grants made through
`grant_tool` (human-approved) persist; allowlisted tools not in TOOLS.md are only reported.
"""

from __future__ import annotations

import inspect
import logging

from langchain_core.tools import BaseTool
from sqlalchemy import select

from aecom.audit import audit
from aecom.db import Allowlist, RegistryTool, session
from aecom.persona.loader import agent_ids, load_spec
from aecom.policy.gate import gate_builtin
from aecom.tools.catalog import CATALOG

log = logging.getLogger(__name__)


def seed_registry() -> dict[str, list[tuple[str, str]]]:
    """Register built-ins and add missing TOOLS.md tools to each agent's allowlist (additive only).

    Returns {"added": [(agent, tool)], "extra": [(agent, tool)]}; *extra* are allowlisted tools that are
    not in TOOLS.md. They are kept, because humans grant and revoke tools at runtime.
    """
    added: list[tuple[str, str]] = []
    extra: list[tuple[str, str]] = []
    with session() as s:
        for name, td in CATALOG.items():
            if s.get(RegistryTool, name) is None:
                s.add(RegistryTool(name=name, kind="builtin", description=(inspect.getdoc(td.fn) or "")[:500],
                                   status="approved", side_effect=td.side_effect, approved_by="system"))
        for agent_id in agent_ids():
            listed = load_spec(agent_id).tools
            for tool_name in listed:
                if tool_name not in CATALOG:
                    raise ValueError(f"{agent_id}/TOOLS.md references unknown tool {tool_name}")
            have = set(s.scalars(select(Allowlist.tool_name).where(Allowlist.agent_id == agent_id)))
            for tool_name in dict.fromkeys(listed):
                if tool_name not in have:
                    s.add(Allowlist(agent_id=agent_id, tool_name=tool_name))
                    added.append((agent_id, tool_name))
            extra += [(agent_id, t) for t in sorted(have - set(listed))]
    for agent_id, tool_name in added:  # after commit: audit() opens its own session
        audit("system", "allowlist_sync", "info", {"agent_id": agent_id, "tool": tool_name})
    # Skills and MCP servers are granted through the registry on purpose (e.g. the Sentinel's runbooks); only a
    # leftover built-in TOOL deserves a warning, because that is what `aecom registry revoke` is for.
    leftovers = [(a, t) for a, t in extra if t in CATALOG]
    if leftovers:
        log.warning("Allowlisted tools not in TOOLS.md (kept; remove with `aecom registry revoke AGENT TOOL --by NAME`): %s",
                    ", ".join(f"{a}/{t}" for a, t in leftovers))
    return {"added": added, "extra": extra}


def allowed_tools(agent_id: str) -> list[str]:
    with session() as s:
        return list(s.scalars(select(Allowlist.tool_name).where(Allowlist.agent_id == agent_id)))


def list_allowlists(agent_id: str | None = None) -> dict[str, list[str]]:
    """{agent_id: [tool, ...]} for every agent with grants (or only `agent_id`)."""
    with session() as s:
        q = select(Allowlist.agent_id, Allowlist.tool_name).order_by(Allowlist.agent_id, Allowlist.tool_name)
        if agent_id:
            q = q.where(Allowlist.agent_id == agent_id)
        out: dict[str, list[str]] = {}
        for a, t in s.execute(q):
            out.setdefault(a, []).append(t)
        return out


def revoke(agent_id: str, tool_name: str, by: str, note: str = "") -> dict:
    """Human-only (CLI, no agent tool): delete the agent's allowlist row for `tool_name` and audit it.

    Returns {"removed": bool, "agent_id", "tool", "warning"?}. The additive sync re-adds tools that the agent's
    TOOLS.md still lists on the next `aecom init` (every CLI command runs it), so the warning says to remove it from TOOLS.md as well."""
    with session() as s:
        row = s.scalar(select(Allowlist).where(Allowlist.agent_id == agent_id, Allowlist.tool_name == tool_name))
        removed = row is not None
        if row is not None:
            s.delete(row)
    out: dict = {"removed": removed, "agent_id": agent_id, "tool": tool_name}
    if removed:
        audit(f"human:{by}", "allowlist_revoke", "revoked", {"agent": agent_id, "tool": tool_name, "note": note})
        try:
            listed = tool_name in load_spec(agent_id).tools
        except Exception:  # unknown agent / unreadable persona: nothing to warn about
            listed = False
        if listed:
            out["warning"] = (f"{agent_id}/TOOLS.md still lists {tool_name}; the next `aecom init` (every CLI command runs it) re-adds it. "
                              "Remove it from TOOLS.md to keep it revoked.")
    return out


def skills_for(agent_id: str) -> list[str]:
    """Approved skills (instruction packs) granted to the agent — appended to its prompt."""
    with session() as s:
        granted = set(s.scalars(select(Allowlist.tool_name).where(Allowlist.agent_id == agent_id)))
        rows = s.scalars(select(RegistryTool).where(RegistryTool.kind == "skill", RegistryTool.status == "approved"))
        return [f"## Skill: {r.name}\n{r.config.get('instructions', '')}" for r in rows if r.name in granted]


async def build_tools(agent_id: str, context: str = "") -> list[BaseTool]:
    """Gated built-ins from the allowlist plus MCP tools of granted servers. An MCP grant is read-only unless a human
    upgraded it to "full" (``grant_tool(level="full")``): read-only grants load only the server's read_only_tools."""
    names = [n for n in allowed_tools(agent_id) if n in CATALOG]
    tools: list[BaseTool] = [gate_builtin(n, agent_id, context) for n in names]
    from aecom.registry.mcp_manager import load_mcp_tools

    try:
        tools += await load_mcp_tools(agent_id)
    except Exception as exc:  # an MCP server being down must not take the agent down
        log.warning("MCP tools unavailable for %s: %s", agent_id, exc)
    return tools
