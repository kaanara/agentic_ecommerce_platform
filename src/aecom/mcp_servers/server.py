"""Company MCP servers (FastMCP, stdio). One implementation, one policy for agents and external clients.

    uv run aecom mcp store        # catalog, inventory, orders
    uv run aecom mcp marketing    # campaigns, social, email
    uv run aecom mcp finance      # P&L, KPIs
    uv run aecom mcp registry     # tools / skills / MCP lifecycle

External callers act as agent ``mcp_client``. Rules that name allowed agents deny them, so in practice
they get the read-only (T0) tools plus anything a human explicitly opens up in policy.yaml.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any

from mcp.server.fastmcp import FastMCP

from aecom.context import current_agent
from aecom.policy.gate import run_gated
from aecom.tools.catalog import CATALOG, ToolDef

DOMAINS = {
    "store": {"store"},
    "marketing": {"marketing", "comms"},
    "finance": {"finance", "supply", "exec"},
    "registry": {"registry"},
    "all": {"store", "marketing", "comms", "finance", "supply", "exec", "registry", "research", "memory"},
}
EXTERNAL_AGENT = "mcp_client"


def _wrap(td: ToolDef) -> Any:
    @functools.wraps(td.fn)
    async def wrapper(**kwargs: Any) -> str:
        def execute(args: dict[str, Any]) -> Any:
            token = current_agent.set(EXTERNAL_AGENT)
            try:
                return td.fn(**args)
            finally:
                current_agent.reset(token)

        return await run_gated(EXTERNAL_AGENT, td.name, kwargs, execute, side_effect=td.side_effect)

    wrapper.__signature__ = inspect.signature(td.fn).replace(return_annotation=str)  # type: ignore[attr-defined]
    return wrapper


def build_server(domain: str) -> FastMCP:
    if domain not in DOMAINS:
        raise ValueError(f"unknown MCP domain {domain}; choose from {sorted(DOMAINS)}")
    server = FastMCP(f"aecom-{domain}")
    for td in CATALOG.values():
        if td.domain in DOMAINS[domain] and not td.terminal:
            server.add_tool(_wrap(td), name=td.name, description=inspect.getdoc(td.fn) or td.name)
    return server


def serve(domain: str) -> None:
    from aecom.bootstrap import init

    init()
    build_server(domain).run()  # stdio transport
