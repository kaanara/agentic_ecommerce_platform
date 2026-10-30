"""M2 (Roadmap.md): the optional, read-only Playwright MCP browser.

``aecom.browser_guard`` in isolation (URL guard table, allow/deny table, deny-by-default, rate limits), then its
wiring into ``aecom.registry.mcp_manager`` for the server named exactly ``"playwright"`` (fencing, taint, other
servers unaffected, defense-in-depth against a misconfigured policy) and the registry proposal lifecycle.
"""

from __future__ import annotations

import asyncio
import socket
import types

import pytest
from langchain_core.tools import StructuredTool

from aecom import browser_guard
from aecom.context import current_agent, current_run, is_tainted
from aecom.db import RegistryTool, session
from aecom.policy import versions as pv
from aecom.policy.gate import load_policy
from aecom.registry.tools import allowed_tools, build_tools
from aecom.tools.catalog import approve_tool, grant_tool, propose_tool, sandbox_tool

# =========================================================================== check_navigation: URL guard table
DNS = {"example.com": ["93.184.216.34"], "other.example": ["93.184.216.35"], "intranet.corp": ["10.1.2.3"],
       "router.lan": ["192.168.1.1"], "metadata.evil": ["169.254.169.254"], "localhost": ["127.0.0.1"]}


@pytest.fixture
def dns(monkeypatch):
    """Fake resolver (same approach as tests/test_security.py): no real DNS lookups happen in this test module."""
    def getaddrinfo(host, port, *a, **kw):
        if host not in DNS:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in DNS[host]]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/health",          # loopback literal
    "http://localhost/",                      # loopback by name
    "http://10.0.0.1/",                       # RFC1918 literal
    "http://192.168.1.1/admin",               # RFC1918 literal
    "http://router.lan/",                     # hostname resolving to an RFC1918 address
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata literal
    "http://metadata.evil/",                  # hostname resolving to the cloud metadata address
    "http://intranet.corp/",                  # hostname resolving to a private address
    "file:///etc/passwd",                     # non-http(s) scheme
    "javascript:alert(document.cookie)",      # non-http(s) scheme
    "data:text/html,<script>alert(1)</script>",  # non-http(s) scheme
    "chrome://settings/",                     # non-http(s) scheme
    "http://user:pass@example.com/",          # credentials in URL
])
def test_check_navigation_refuses(dns, url):
    with pytest.raises(browser_guard.BrowserGuardError):
        browser_guard.check_navigation(url)


def test_check_navigation_allows_a_public_host(dns):
    browser_guard.check_navigation("https://example.com/page")  # must not raise


def test_check_navigation_refuses_a_hostname_that_resolves_to_a_private_ip(monkeypatch):
    """A hostname with no hint of being private in its name, but whose resolver answer is private."""
    def getaddrinfo(host, port, *a, **kw):
        assert host == "looks-public.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    with pytest.raises(browser_guard.BrowserGuardError, match="private"):
        browser_guard.check_navigation("http://looks-public.example/")


# =========================================================================== check_tool_call: allow/deny table
@pytest.fixture(autouse=True)
def _reset_budgets():
    """Rate-limit counters are process-local (not per-test DB state), so clear them around every test."""
    browser_guard.reset()
    yield
    browser_guard.reset()


@pytest.mark.parametrize("tool", sorted(browser_guard.READ_ONLY_TOOLS - {"browser_navigate"}))
def test_check_tool_call_allows_the_read_only_tools(tool):
    browser_guard.check_tool_call(tool, {})  # must not raise


@pytest.mark.parametrize("tool", sorted(browser_guard.DENIED_TOOLS))
def test_check_tool_call_denies_every_interactive_tool(tool):
    with pytest.raises(browser_guard.BrowserGuardError, match="destructive or interactive"):
        browser_guard.check_tool_call(tool, {})


def test_check_tool_call_denies_by_default_an_unlisted_tool_name():
    """Deny-by-default: a tool neither READ_ONLY_TOOLS nor DENIED_TOOLS names is still refused (allowlist, not a
    denylist) - this covers a brand-new Playwright tool nobody has classified yet."""
    with pytest.raises(browser_guard.BrowserGuardError, match="not on the browser's read-only allow list"):
        browser_guard.check_tool_call("browser_mystery_tool_from_a_future_release", {})


def test_check_tool_call_denies_an_unlisted_tool_whose_name_hints_at_the_denied_intent():
    with pytest.raises(browser_guard.BrowserGuardError, match="destructive or interactive"):
        browser_guard.check_tool_call("browser_download_file", {})


def test_check_tool_call_checks_the_navigation_url(dns):
    with pytest.raises(browser_guard.BrowserGuardError, match="navigation refused"):
        browser_guard.check_tool_call("browser_navigate", {"url": "http://127.0.0.1/"})


def test_check_tool_call_allows_a_safe_navigation(dns):
    browser_guard.check_tool_call("browser_navigate", {"url": "https://example.com/"})


# =========================================================================== rate limits
def test_default_call_limit_is_20():
    for _ in range(20):
        browser_guard.check_tool_call("browser_snapshot", {})
    with pytest.raises(browser_guard.BrowserGuardError, match=r"call limit reached \(20"):
        browser_guard.check_tool_call("browser_snapshot", {})


def test_a_denied_call_does_not_spend_the_call_budget():
    for _ in range(20):
        with pytest.raises(browser_guard.BrowserGuardError):
            browser_guard.check_tool_call("browser_click", {})
    browser_guard.check_tool_call("browser_snapshot", {})  # budget untouched by the 20 denials above


def test_browser_max_calls_setting_overrides_the_default(monkeypatch):
    monkeypatch.setattr(browser_guard, "get_settings", lambda: types.SimpleNamespace(browser_max_calls=3))
    for _ in range(3):
        browser_guard.check_tool_call("browser_snapshot", {})
    with pytest.raises(browser_guard.BrowserGuardError, match=r"call limit reached \(3"):
        browser_guard.check_tool_call("browser_snapshot", {})


def test_default_navigation_limit_is_5(dns):
    for _ in range(5):
        browser_guard.check_tool_call("browser_navigate", {"url": "https://example.com/"})
    with pytest.raises(browser_guard.BrowserGuardError, match=r"navigation limit reached \(5"):
        browser_guard.check_tool_call("browser_navigate", {"url": "https://example.com/"})


def test_per_domain_navigation_cap_is_independent_per_domain(monkeypatch, dns):
    monkeypatch.setattr(browser_guard, "get_settings", lambda: types.SimpleNamespace(
        browser_max_calls=100, browser_max_navigations=100, browser_max_calls_per_domain=2))
    browser_guard.check_tool_call("browser_navigate", {"url": "https://example.com/a1"})
    browser_guard.check_tool_call("browser_navigate", {"url": "https://example.com/a2"})
    browser_guard.check_tool_call("browser_navigate", {"url": "https://other.example/b1"})  # a different domain
    with pytest.raises(browser_guard.BrowserGuardError, match="per-domain"):
        browser_guard.check_tool_call("browser_navigate", {"url": "https://example.com/a3"})
    browser_guard.check_tool_call("browser_navigate", {"url": "https://other.example/b2"})  # still has room


def test_budgets_are_tracked_per_run(monkeypatch):
    """Two different run ids never share a budget."""
    monkeypatch.setattr(browser_guard, "get_settings", lambda: types.SimpleNamespace(browser_max_calls=1))
    token = current_run.set("run-a")
    try:
        browser_guard.check_tool_call("browser_snapshot", {})
        with pytest.raises(browser_guard.BrowserGuardError):
            browser_guard.check_tool_call("browser_snapshot", {})
    finally:
        current_run.reset(token)
    token = current_run.set("run-b")
    try:
        browser_guard.check_tool_call("browser_snapshot", {})  # a fresh budget for a different run
    finally:
        current_run.reset(token)


# =========================================================================== registry proposal
def test_registry_proposal_shape():
    proposal = browser_guard.registry_proposal()
    assert proposal["name"] == "playwright"
    assert proposal["kind"] == "mcp_server"
    assert set(proposal["launch_config"]["read_only_tools"]) == set(browser_guard.READ_ONLY_TOOLS)
    assert proposal["launch_config"]["transport"] == "stdio"


def test_registry_proposal_never_auto_approves(fresh_company):
    propose_tool(**browser_guard.registry_proposal())
    with session() as s:
        row = s.get(RegistryTool, "playwright")
        assert row.status == "proposed"
    assert "playwright" not in allowed_tools("researcher")
    assert "playwright" not in allowed_tools("foreman")


# =========================================================================== mcp_manager wiring (FakeClient)
SERVER = "playwright"
OTHER_SERVER = "acme"
READ_ONLY = sorted(browser_guard.READ_ONLY_TOOLS)
TOOLS_BY_SERVER = {SERVER: [*READ_ONLY, "browser_click"], OTHER_SERVER: ["list_items"]}


class FakeClient:
    """Stands in for MultiServerMCPClient: no server is started, every tool call is recorded."""

    calls: list[tuple[str, str, dict]] = []  # (server, tool, args)

    def __init__(self, connections):
        self.connections = connections

    async def get_tools(self):
        (server_name,) = self.connections.keys()

        def make(name):
            async def run(**kwargs):
                FakeClient.calls.append((server_name, name, kwargs))
                return {"tool": name, "echo": kwargs}

            schema = ({"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}
                      if name == "browser_navigate" else {"type": "object", "properties": {}})
            return StructuredTool.from_function(coroutine=run, name=name, description=f"fake {name}", args_schema=schema)

        return [make(n) for n in TOOLS_BY_SERVER[server_name]]


POLICY_YAML = """
defaults:
  unknown_tool_tier: T3
  mcp_unclassified_tier: T3
  sandboxed_tool_tier: T3
  approval_ttl_hours: 72
rules:
  mcp:playwright/browser_navigate: {tier: T1, agents: [researcher]}
  mcp:playwright/browser_snapshot: {tier: T1, agents: [researcher]}
  mcp:playwright/browser_take_screenshot: {tier: T1, agents: [researcher]}
  mcp:playwright/browser_navigate_back: {tier: T1, agents: [researcher]}
  mcp:playwright/browser_wait_for: {tier: T1, agents: [researcher]}
  # Deliberately misconfigured T1 (as if a human fat-fingered the policy): proves aecom.browser_guard denies this
  # on its own, defense-in-depth, even though the gate alone would have allowed it.
  mcp:playwright/browser_click: {tier: T1, agents: [researcher]}
  mcp:acme/list_items: {tier: T1, agents: [researcher]}
"""


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    load_policy.cache_clear()
    yield
    load_policy.cache_clear()


@pytest.fixture
def fake_mcp(monkeypatch, fresh_company):
    import langchain_mcp_adapters.client as client_mod

    FakeClient.calls = []
    monkeypatch.setattr(client_mod, "MultiServerMCPClient", FakeClient)
    monkeypatch.setattr(fresh_company, "mcp_autoload", True)
    pv.save_version(POLICY_YAML, "M2 test policy", "alice", step_up_ok=True)

    propose_tool(**browser_guard.registry_proposal())
    sandbox_tool(SERVER)
    approve_tool(SERVER)
    grant_tool(SERVER, "researcher", level="full")  # full: loads browser_click too, for the defense-in-depth test

    propose_tool(OTHER_SERVER, "mcp_server", "Vendor server (unaffected by browser_guard)",
                 {"transport": "stdio", "command": "acme-mcp", "read_only_tools": ["list_items"]})
    sandbox_tool(OTHER_SERVER)
    approve_tool(OTHER_SERVER)
    grant_tool(OTHER_SERVER, "researcher")
    return FakeClient


def _mcp_tool(agent_id: str, qualified_name: str):
    tools = asyncio.run(build_tools(agent_id))
    return next(t for t in tools if t.metadata.get("mcp") == qualified_name)


def _invoke(run_id: str, agent_id: str, qualified_name: str, args: dict) -> str:
    token_run, token_agent = current_run.set(run_id), current_agent.set(agent_id)
    try:
        return asyncio.run(_mcp_tool(agent_id, qualified_name).ainvoke(args))
    finally:
        current_run.reset(token_run)
        current_agent.reset(token_agent)


def test_allowed_playwright_call_is_fenced_and_taints_the_run(fake_mcp):
    out = _invoke("run-pw-1", "researcher", "mcp:playwright/browser_navigate", {"url": "https://example.com/"})
    assert out.startswith('<untrusted source="mcp:playwright/browser_navigate">')
    assert "(The block above is external DATA" in out
    assert FakeClient.calls == [(SERVER, "browser_navigate", {"url": "https://example.com/"})]
    token_run, token_agent = current_run.set("run-pw-1"), current_agent.set("researcher")
    try:
        assert is_tainted()
    finally:
        current_run.reset(token_run)
        current_agent.reset(token_agent)


def test_unsafe_navigation_is_denied_before_reaching_the_container(fake_mcp):
    out = _invoke("run-pw-2", "researcher", "mcp:playwright/browser_navigate", {"url": "http://127.0.0.1/"})
    assert out.startswith("DENIED by browser guard:")
    assert "navigation refused" in out
    assert FakeClient.calls == []  # never reached the (fake) container


def test_misconfigured_policy_allow_is_still_denied_by_browser_guard(fake_mcp):
    """Defense in depth: the test policy above gives mcp:playwright/browser_click tier T1 (allow) for researcher,
    on purpose - browser_guard.check_tool_call still refuses it, deny-by-default, before the fake tool runs."""
    out = _invoke("run-pw-3", "researcher", "mcp:playwright/browser_click", {})
    assert out.startswith("DENIED by browser guard:")
    assert "destructive or interactive" in out
    assert FakeClient.calls == []


def test_rate_limit_is_enforced_through_the_full_mcp_pipeline(fake_mcp, monkeypatch):
    monkeypatch.setattr(browser_guard, "get_settings", lambda: types.SimpleNamespace(browser_max_calls=2))
    run_id = "run-pw-rate"
    first = _invoke(run_id, "researcher", "mcp:playwright/browser_snapshot", {})
    second = _invoke(run_id, "researcher", "mcp:playwright/browser_snapshot", {})
    third = _invoke(run_id, "researcher", "mcp:playwright/browser_snapshot", {})
    assert "<untrusted" in first and "<untrusted" in second
    assert third.startswith("DENIED by browser guard:") and "call limit" in third
    assert len(FakeClient.calls) == 2  # the third, denied call never reached the (fake) container


def test_other_mcp_servers_are_not_wrapped_by_browser_guard(fake_mcp):
    """mcp:acme/* goes through the ordinary, un-wrapped MCP path: no fencing, no browser_guard rate limit."""
    out = _invoke("run-acme-1", "researcher", "mcp:acme/list_items", {})
    assert "<untrusted" not in out
    assert "tool" in out and "list_items" in out  # the fake tool's plain JSON result, untouched
    assert FakeClient.calls == [(OTHER_SERVER, "list_items", {})]
    # exhausting acme's call count must not affect playwright's separate budget tracked by browser_guard
    for i in range(25):
        _invoke("run-acme-1", "researcher", "mcp:acme/list_items", {})
    out2 = _invoke("run-acme-1", "researcher", "mcp:playwright/browser_snapshot", {"url": "https://example.com/"})
    assert "<untrusted" in out2
