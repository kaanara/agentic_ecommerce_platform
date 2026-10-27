"""E2: vendor MCP onboarding - a granted MCP server exposes its read-only tools first; "full" is a human step."""

import asyncio

import pytest
from langchain_core.tools import StructuredTool
from sqlalchemy import select

from aecom import approvals
from aecom.db import Approval, RegistryTool, session
from aecom.policy.gate import gate_builtin
from aecom.registry import mcp_manager
from aecom.registry.tools import build_tools
from aecom.tools.catalog import approve_tool, grant_tool, propose_tool, sandbox_tool

SERVER = "acme-shipping"
READ_ONLY = ["track_parcel", "list_rates"]
ALL_TOOLS = [*READ_ONLY, "create_label", "login_portal"]


class FakeClient:
    """Stands in for MultiServerMCPClient: no server is started, every tool records its calls."""

    calls: list[tuple[str, dict]] = []

    def __init__(self, connections):
        self.connections = connections

    async def get_tools(self):
        def make(name):
            async def run(**kwargs):
                FakeClient.calls.append((name, kwargs))
                return f"{name} ok"

            return StructuredTool.from_function(coroutine=run, name=name, description=f"fake {name}",
                                                args_schema={"type": "object", "properties": {}})

        return [make(n) for n in ALL_TOOLS]


@pytest.fixture
def fake_mcp(monkeypatch, fresh_company):
    import langchain_mcp_adapters.client as client_mod

    FakeClient.calls = []
    monkeypatch.setattr(client_mod, "MultiServerMCPClient", FakeClient)
    monkeypatch.setattr(fresh_company, "mcp_autoload", True)
    propose_tool(SERVER, "mcp_server", "Vendor shipping API",
                 {"transport": "stdio", "command": "acme-mcp", "read_only_tools": READ_ONLY})
    sandbox_tool(SERVER)
    approve_tool(SERVER)
    return FakeClient


def _mcp_names(agent_id: str) -> set[str]:
    tools = asyncio.run(build_tools(agent_id))
    return {t.name for t in tools if t.metadata.get("mcp")}


def _mcp_tool(agent_id: str, name: str):
    return next(t for t in asyncio.run(build_tools(agent_id)) if t.metadata.get("mcp") and t.name == name)


def test_read_only_grant_is_the_default_and_exposes_only_read_only_tools(fake_mcp):
    assert grant_tool(SERVER, "supply")["level"] == "read_only"
    assert _mcp_names("supply") == set(READ_ONLY)
    with session() as s:
        assert s.get(RegistryTool, SERVER).config["grants"] == {"supply": "read_only"}
    assert _mcp_names("support") == set()  # not granted at all


def test_full_grant_exposes_all_tools_still_gated(fake_mcp):
    grant_tool(SERVER, "supply", level="full")
    assert _mcp_names("supply") == set(ALL_TOOLS)
    out = asyncio.run(_mcp_tool("supply", "create_label").ainvoke({}))
    assert out.startswith("ESCALATED") and "T3" in out  # side effect without a policy rule stays T3
    assert fake_mcp.calls == []
    assert asyncio.run(_mcp_tool("supply", "track_parcel").ainvoke({})) == "track_parcel ok"


def test_forbidden_patterns_deny_login_tools_even_with_full_grant(fake_mcp):
    grant_tool(SERVER, "supply", level="full")
    out = asyncio.run(_mcp_tool("supply", "login_portal").ainvoke({}))
    assert out.startswith("DENIED by policy (T4)")
    assert fake_mcp.calls == []


def test_upgrade_to_full_needs_human_approval(fake_mcp):
    grant_tool(SERVER, "supply")
    gated = gate_builtin("grant_tool", "foreman")
    out = asyncio.run(gated.ainvoke({"tool_name": SERVER, "agent_id": "supply", "level": "full"}))
    assert out.startswith("ESCALATED") and "T3" in out
    assert _mcp_names("supply") == set(READ_ONLY)  # nothing changes until a human approves
    with session() as s:
        a = s.scalars(select(Approval).where(Approval.tool_name == "grant_tool")).one()
        approval_id = a.id
    assert approvals.approve(approval_id, "cto-human")["status"] == "executed"
    assert _mcp_names("supply") == set(ALL_TOOLS)


def test_invalid_level_is_rejected(fake_mcp):
    with pytest.raises(ValueError, match="level"):
        grant_tool(SERVER, "supply", level="admin")


def test_parked_call_cannot_widen_a_read_only_grant(fake_mcp):
    from aecom.context import current_agent

    grant_tool(SERVER, "supply")
    token = current_agent.set("supply")
    try:
        with pytest.raises(PermissionError, match="read-only"):
            asyncio.run(mcp_manager.call_mcp_tool(SERVER, "create_label", {}))
        assert asyncio.run(mcp_manager.call_mcp_tool(SERVER, "track_parcel", {})) == "track_parcel ok"
    finally:
        current_agent.reset(token)
    assert fake_mcp.calls == [("track_parcel", {})]


def test_reproposal_cannot_smuggle_a_full_grant(fake_mcp):
    grant_tool(SERVER, "supply", level="full")
    # an agent re-proposes the server with grants in launch_config: the Allowlist row stays, the level must not
    propose_tool(SERVER, "mcp_server", "Vendor shipping API v2",
                 {"transport": "stdio", "command": "acme-mcp", "read_only_tools": READ_ONLY,
                  "grants": {"supply": "full", "support": "full"}})
    sandbox_tool(SERVER)
    approve_tool(SERVER)
    with session() as s:
        assert "grants" not in s.get(RegistryTool, SERVER).config
    assert _mcp_names("supply") == set(READ_ONLY)  # back to read-only after the re-proposal


def test_new_proposal_drops_agent_supplied_grants(fake_mcp):
    propose_tool("other-vendor", "mcp_server", "x", {"command": "x", "read_only_tools": [], "grants": {"supply": "full"}})
    with session() as s:
        assert "grants" not in s.get(RegistryTool, "other-vendor").config
