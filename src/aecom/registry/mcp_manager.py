"""Connects registry MCP servers to agents via langchain-mcp-adapters.

- approved servers   → tools for every agent on the allowlist, filtered by that agent's grant level
- sandboxed servers  → tools for the Foreman only, every side-effecting call escalated
Server config (registry_tools.config): {"transport": "stdio", "command": "...", "args": [...],
"read_only_tools": ["..."]}  or  {"transport": "streamable_http", "url": "..."}.

Grant levels live in the same config, ``config["grants"][agent_id]``: "read_only" (the default, also for grants
that predate levels) loads only ``read_only_tools``; "full" loads every tool, still gated (side-effect tools
without a policy rule are T3). Upgrading to "full" is ``grant_tool(level="full")``, which is T3 (human).

M2 (Roadmap.md, ``aecom.browser_guard``): the server named exactly ``"playwright"`` gets one extra wrap, here and
only here — every other server's tools go through the gate unchanged. ``_wrap_playwright_tool`` runs
``browser_guard.check_tool_call`` before the underlying Playwright MCP tool is actually invoked (denying a call
the policy tier alone can't see, e.g. a private-network URL or a budget overrun, with a clear message instead of
an exception trace) and, for an allowed call, treats the result as untrusted web content: ``catalog.fence`` wraps
it and marks the run tainted (I15), exactly like ``fetch_url`` does for a public page.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from sqlalchemy import select

from aecom import browser_guard
from aecom.audit import audit
from aecom.config import get_settings
from aecom.context import current_agent
from aecom.db import Allowlist, RegistryTool, session
from aecom.policy.gate import gate_external
from aecom.tools.catalog import fence

log = logging.getLogger(__name__)

GRANT_LEVELS = ("read_only", "full")

PLAYWRIGHT_SERVER = "playwright"  # the one MCP server browser_guard applies to (M2)


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


def _stringify(result: Any) -> str:
    return result if isinstance(result, str) else json.dumps(result, default=str)


def _wrap_playwright_tool(t: BaseTool) -> BaseTool:
    """M2: wrap one Playwright MCP tool so ``load_mcp_tools`` can gate it exactly like any other MCP tool
    afterwards, while adding the two checks a policy tier alone can't express. Same name/description/args_schema
    as ``t`` (so the model still sees the real Playwright tool signature), but its coroutine:

    1. runs ``browser_guard.check_tool_call`` *before* the real tool is invoked, denying with a clear, model-facing
       message (deny-by-default for any tool not on the read-only allow list, URL/SSRF checks for navigation,
       per-run and per-domain rate limits) instead of letting the call reach the container;
    2. on an allowed call, fences the result as untrusted web content (``aecom.tools.catalog.fence``), which also
       marks the run tainted (I15) — the same treatment ``fetch_url`` gives a public page.
    """

    async def _call(**kwargs: Any) -> str:
        try:
            browser_guard.check_tool_call(t.name, kwargs)
        except browser_guard.BrowserGuardError as exc:
            return f"DENIED by browser guard: {exc}"
        result = await t.ainvoke(kwargs)
        return fence(f"mcp:{PLAYWRIGHT_SERVER}/{t.name}", _stringify(result))

    return StructuredTool(name=t.name, description=t.description, args_schema=t.args_schema, coroutine=_call)


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
            if name == PLAYWRIGHT_SERVER:  # M2: browser_guard + fence, this server only
                t = _wrap_playwright_tool(t)
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
            if server == PLAYWRIGHT_SERVER:  # M2: same guard + fence as the live path, this server only
                browser_guard.check_tool_call(t.name, args)
                return fence(f"mcp:{PLAYWRIGHT_SERVER}/{t.name}", _stringify(await t.ainvoke(args)))
            return await t.ainvoke(args)
    raise ValueError(f"{server} has no tool {tool}")
