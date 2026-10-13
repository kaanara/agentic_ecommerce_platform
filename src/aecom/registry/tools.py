"""Tool/skill/MCP registry: the source of truth for *what exists* and *who may use it*.

On first init, built-ins are registered and allowlists are seeded from each agent's TOOLS.md.
After that the database wins, so grants made through `grant_tool` (human-approved) persist.
"""

from __future__ import annotations

import inspect
import logging

from langchain_core.tools import BaseTool
from sqlalchemy import select

from aecom.db import Allowlist, RegistryTool, session
from aecom.persona.loader import agent_ids, load_spec
from aecom.policy.gate import gate_builtin
from aecom.tools.catalog import CATALOG

log = logging.getLogger(__name__)


def seed_registry() -> None:
    with session() as s:
        for name, td in CATALOG.items():
            if s.get(RegistryTool, name) is None:
                s.add(RegistryTool(name=name, kind="builtin", description=(inspect.getdoc(td.fn) or "")[:500],
                                   status="approved", side_effect=td.side_effect, approved_by="system"))
        seeded = s.scalars(select(Allowlist.agent_id).distinct()).all()
        for agent_id in agent_ids():
            if agent_id in seeded:
                continue
            for tool_name in load_spec(agent_id).tools:
                if tool_name not in CATALOG:
                    raise ValueError(f"{agent_id}/TOOLS.md references unknown tool {tool_name}")
                s.add(Allowlist(agent_id=agent_id, tool_name=tool_name))


def allowed_tools(agent_id: str) -> list[str]:
    with session() as s:
        return list(s.scalars(select(Allowlist.tool_name).where(Allowlist.agent_id == agent_id)))


def skills_for(agent_id: str) -> list[str]:
    """Approved skills (instruction packs) granted to the agent — appended to its prompt."""
    with session() as s:
        granted = set(s.scalars(select(Allowlist.tool_name).where(Allowlist.agent_id == agent_id)))
        rows = s.scalars(select(RegistryTool).where(RegistryTool.kind == "skill", RegistryTool.status == "approved"))
        return [f"## Skill: {r.name}\n{r.config.get('instructions', '')}" for r in rows if r.name in granted]


async def build_tools(agent_id: str, context: str = "") -> list[BaseTool]:
    names = [n for n in allowed_tools(agent_id) if n in CATALOG]
    tools: list[BaseTool] = [gate_builtin(n, agent_id, context) for n in names]
    from aecom.registry.mcp_manager import load_mcp_tools

    try:
        tools += await load_mcp_tools(agent_id)
    except Exception as exc:  # an MCP server being down must not take the agent down
        log.warning("MCP tools unavailable for %s: %s", agent_id, exc)
    return tools
