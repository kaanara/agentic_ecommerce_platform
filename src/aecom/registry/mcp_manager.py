"""Connects registry MCP servers to agents via langchain-mcp-adapters.

- approved servers   → tools for every agent on the allowlist, filtered by that agent's grant level
- sandboxed servers  → tools for the Foreman only, every side-effecting call escalated
Server config (registry_tools.config): {"transport": "stdio", "command": "...", "args": [...],
"read_only_tools": ["..."]}  or  {"transport": "streamable_http", "url": "..."}.

Grant levels live in the same config, ``config["grants"][agent_id]``: "read_only" (the default, also for grants
that predate levels) loads only ``read_only_tools``; "full" loads every tool, still gated (side-effect tools
without a policy rule are T3). Upgrading to "full" is ``grant_tool(level="full")``, which is T3 (human).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool
from sqlalchemy import select

from aecom.audit import audit
from aecom.config import get_settings
from aecom.context import current_agent
from aecom.db import Allowlist, RegistryTool, session
from aecom.policy.gate import gate_external

log = logging.getLogger(__name__)

GRANT_LEVELS = ("read_only", "full")


def grant_level(cfg: dict[str, Any], agent_id: str) -> str:
    """The agent's grant level on an MCP server config; anything but "full" counts as read-only."""
    return "full" if (cfg.get("grants") or {}).get(agent_id) == "full" else "read_only"


def set_grant_level(server: str, agent_id: str, level: str) -> None:
    """Record ``level`` for ``agent_id`` on an MCP server registry entry (no-op for other kinds)."""
    if level not in GRANT_LEVELS:
        raise ValueError(f"level must be one of {', '.join(GRANT_LEVELS)}")
    with session() as s:
        row = s.get(RegistryTool, server)
        if row is None or row.kind != "mcp_server":
            return
        cfg = dict(row.config or {})
        cfg["grants"] = {**(cfg.get("grants") or {}), agent_id: level}
        row.config = cfg  # new dict so the JSON column is marked dirty


def visible_tools(cfg: dict[str, Any], agent_id: str, names: list[str], *, sandboxed: bool) -> list[str]:
    """Tool names of one server the agent may load: a read-only grant sees only ``read_only_tools``."""
    if sandboxed or grant_level(cfg, agent_id) == "full":
        return list(names)
    read_only = set(cfg.get("read_only_tools", []))
    return [n for n in names if n in read_only]


def _connection(cfg: dict[str, Any]) -> dict[str, Any]:
    transport = cfg.get("transport", "stdio")
    if transport == "stdio":
        return {"transport": "stdio", "command": cfg["command"], "args": cfg.get("args", []), "env": cfg.get("env")}
    return {"transport": transport, "url": cfg["url"]}


def servers_for(agent_id: str) -> list[tuple[str, dict[str, Any], bool]]:
    with session() as s:
        granted = set(s.scalars(select(Allowlist.tool_name).where(Allowlist.agent_id == agent_id)))
        out = []
        for r in s.scalars(select(RegistryTool).where(RegistryTool.kind == "mcp_server")):
            if r.status == "approved" and r.name in granted:
                out.append((r.name, dict(r.config), False))
            elif r.status == "sandboxed" and agent_id == "foreman":
                out.append((r.name, dict(r.config), True))
        return out


async def load_mcp_tools(agent_id: str) -> list[BaseTool]:
    if not get_settings().mcp_autoload:
        return []
    servers = servers_for(agent_id)
    if not servers:
        return []
    from langchain_mcp_adapters.client import MultiServerMCPClient

    tools: list[BaseTool] = []
    for name, cfg, sandboxed in servers:
        try:
            client = MultiServerMCPClient({name: _connection(cfg)})
            loaded = await asyncio.wait_for(client.get_tools(), timeout=get_settings().mcp_load_timeout_s)
        except Exception as exc:
            audit("system", f"mcp_load:{name}", "error", {"error": str(exc)})
            continue
        read_only = set(cfg.get("read_only_tools", []))
        allowed = set(visible_tools(cfg, agent_id, [t.name for t in loaded], sandboxed=sandboxed))
        for t in loaded:
            if t.name not in allowed:
                continue
            tools.append(gate_external(t, agent_id, f"mcp:{name}/{t.name}", side_effect=t.name not in read_only,
                                       sandboxed=sandboxed))
    return tools


async def call_mcp_tool(server: str, tool: str, args: dict[str, Any]) -> Any:
    """Used when a human approves a parked MCP call (``current_agent`` is the agent that asked).

    Refuses tools outside the agent's grant level on an approved server, so an approval can't widen a read-only
    grant."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    with session() as s:
        row = s.get(RegistryTool, server)
        if row is None:
            raise ValueError(f"unknown MCP server {server}")
        cfg, status = dict(row.config), row.status
    agent_id = current_agent.get()
    if status == "approved" and not visible_tools(cfg, agent_id, [tool], sandboxed=False):
        raise PermissionError(f"{agent_id} has a read-only grant on {server}; {tool} needs a full grant")
    client = MultiServerMCPClient({server: _connection(cfg)})
    for t in await client.get_tools():
        if t.name == tool:
            return await t.ainvoke(args)
    raise ValueError(f"{server} has no tool {tool}")
