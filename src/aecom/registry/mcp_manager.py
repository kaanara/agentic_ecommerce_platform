"""Connects registry MCP servers to agents via langchain-mcp-adapters.

- approved servers   → tools for every agent on the allowlist
- sandboxed servers  → tools for the Foreman only, every side-effecting call escalated
Server config (registry_tools.config): {"transport": "stdio", "command": "...", "args": [...],
"read_only_tools": ["..."]}  or  {"transport": "streamable_http", "url": "..."}.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import BaseTool
from sqlalchemy import select

from aecom.audit import audit
from aecom.config import get_settings
from aecom.db import Allowlist, RegistryTool, session
from aecom.policy.gate import gate_external

log = logging.getLogger(__name__)


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
        for t in loaded:
            tools.append(gate_external(t, agent_id, f"mcp:{name}/{t.name}", side_effect=t.name not in read_only,
                                       sandboxed=sandboxed))
    return tools


async def call_mcp_tool(server: str, tool: str, args: dict[str, Any]) -> Any:
    """Used when a human approves a parked MCP call."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    with session() as s:
        row = s.get(RegistryTool, server)
        if row is None:
            raise ValueError(f"unknown MCP server {server}")
        cfg = dict(row.config)
    client = MultiServerMCPClient({server: _connection(cfg)})
    for t in await client.get_tools():
        if t.name == tool:
            return await t.ainvoke(args)
    raise ValueError(f"{server} has no tool {tool}")
