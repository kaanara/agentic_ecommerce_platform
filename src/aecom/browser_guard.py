"""M2 (Roadmap.md): guard rail for the optional, read-only Playwright MCP browser.

The policy gate (``aecom.policy.gate``) already gives ``mcp:playwright/<tool>`` calls a tier (T1 for the
read-only tools, T4 — a hard deny — for anything destructive or interactive; see the policy patch in
docs/wiki/Browser-Access.md). This module is the extra, code-level layer ``aecom.registry.mcp_manager`` runs
*after* the gate's T1 "allow" and *before* the call actually reaches the container, because a YAML tier can't
express "not this URL" or "not this many calls":

- ``check_navigation(url)`` — SSRF-style guard for the URL a ``browser_navigate`` call would open. Delegates to
  ``aecom.net_guard.resolve_public``, so it rejects exactly what that module rejects: non-http(s) schemes
  (``file:``, ``javascript:``, ``data:``, ``chrome:``, ...), URLs carrying credentials, and any hostname/IP that
  resolves to a loopback, RFC1918-private, link-local (including the ``169.254.169.254`` cloud-metadata address),
  multicast, reserved or unspecified address — which is also what keeps the browser off the Raspberry Pi's own
  LAN and the Pi itself, on any port, since every one of those ranges is private or loopback.
- ``check_tool_call(tool, args)`` — deny-by-default allowlist of the handful of read-only Playwright tools M2
  permits (navigate, snapshot, screenshot, back, wait), a per-run call budget, a per-run navigation budget and a
  per-run-per-domain navigation cap, tracked by ``aecom.context.current_run``.

Both raise ``BrowserGuardError`` (a ``PermissionError``) with a short, model-facing reason; nothing here mutates
the database or talks to a network — the counters are in-process only; a web-concurrent process (multiple
workers) would get independent budgets, which only ever makes the limit stricter, never looser, so that's an
acceptable degradation for a low-traffic optional capability, not a security hole.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from aecom.config import get_settings
from aecom.context import current_run

DEFAULT_MAX_CALLS = 20          # getattr(get_settings(), "browser_max_calls", 20) overrides this
DEFAULT_MAX_NAVIGATIONS = 5     # getattr(get_settings(), "browser_max_navigations", 5) overrides this
DEFAULT_MAX_PER_DOMAIN = 10     # getattr(get_settings(), "browser_max_calls_per_domain", 10) overrides this

# M2: read-only Playwright MCP tools (https://github.com/microsoft/playwright-mcp) the policy allows. Everything
# else is denied, whether or not it is named below - this is an allowlist, not a denylist.
READ_ONLY_TOOLS = frozenset({
    "browser_navigate", "browser_snapshot", "browser_take_screenshot", "browser_navigate_back", "browser_wait_for",
})

# Named here only so a DENIED message can say *why*; `check_tool_call` denies any tool not in READ_ONLY_TOOLS
# regardless of whether it is listed below (deny-by-default), so this set does not need to be exhaustive.
DENIED_TOOLS = frozenset({
    "browser_click", "browser_type", "browser_fill_form", "browser_press_key", "browser_select_option",
    "browser_file_upload", "browser_evaluate", "browser_run_code", "browser_install", "browser_drag",
    "browser_hover", "browser_handle_dialog", "browser_tabs",
})

# Substrings that make a tool name's *intent* obvious even if Playwright ships a tool neither list above knows
# about yet (a future `browser_download_file`, `browser_login`, ...): still denied, by name, deny-by-default.
_DENY_NAME_HINTS = ("download", "upload", "login", "sign_in", "signin", "submit", "purchase", "buy", "checkout", "pay")


class BrowserGuardError(PermissionError):
    """A Playwright MCP call must be refused before it reaches the browser; ``str(exc)`` is model-facing."""


# run_id -> {"calls": int, "navigations": int, "domains": Counter[str]}; process-local, reset per run id.
_run_state: dict[str, dict[str, Any]] = {}


def _run_id() -> str:
    return current_run.get() or "no-run"


def _state(run_id: str) -> dict[str, Any]:
    return _run_state.setdefault(run_id, {"calls": 0, "navigations": 0, "domains": Counter()})


def reset(run_id: str | None = None) -> None:
    """Clear rate-limit counters for one run, or every run (tests; a run id is never reused in production)."""
    if run_id is None:
        _run_state.clear()
    else:
        _run_state.pop(run_id, None)


def check_navigation(url: str) -> None:
    """Raise ``BrowserGuardError`` unless ``url`` is safe for the browser to open (see module docstring)."""
    from aecom.net_guard import UnsafeURL, resolve_public

    try:
        resolve_public(url)
    except UnsafeURL as exc:
        raise BrowserGuardError(f"navigation refused: {exc}") from exc


def check_tool_call(tool: str, args: dict[str, Any]) -> None:
    """Raise ``BrowserGuardError`` unless ``tool`` is one of the read-only Playwright tools, the call's URL (for
    ``browser_navigate``) is safe, and the per-run/per-domain budgets still have room. Call budgets are only spent
    once every check has passed, so a denied call never counts against them."""
    if tool not in READ_ONLY_TOOLS:
        if tool in DENIED_TOOLS or any(hint in tool.lower() for hint in _DENY_NAME_HINTS):
            reason = "is a destructive or interactive browser action (click, type, form submit, download, login, ...)"
        else:
            reason = "is not on the browser's read-only allow list"
        raise BrowserGuardError(f"{tool} {reason}; this Playwright browser is read-only "
                                "(navigate, snapshot, screenshot, back, wait only).")

    settings = get_settings()
    max_calls = int(getattr(settings, "browser_max_calls", DEFAULT_MAX_CALLS) or DEFAULT_MAX_CALLS)
    max_navigations = int(getattr(settings, "browser_max_navigations", DEFAULT_MAX_NAVIGATIONS) or DEFAULT_MAX_NAVIGATIONS)
    max_per_domain = int(getattr(settings, "browser_max_calls_per_domain", DEFAULT_MAX_PER_DOMAIN) or DEFAULT_MAX_PER_DOMAIN)

    st = _state(_run_id())
    if st["calls"] >= max_calls:
        raise BrowserGuardError(f"browser call limit reached ({max_calls} calls this run); ask a human to continue.")

    domain = None
    if tool == "browser_navigate":
        url = str(args.get("url", ""))
        check_navigation(url)
        if st["navigations"] >= max_navigations:
            raise BrowserGuardError(f"browser navigation limit reached ({max_navigations} navigations this run); "
                                    "ask a human to continue.")
        domain = (urlsplit(url).hostname or "").lower()
        if st["domains"][domain] >= max_per_domain:
            raise BrowserGuardError(f"per-domain navigation limit reached ({max_per_domain} calls to {domain} "
                                    "this run); ask a human to continue.")

    st["calls"] += 1
    if tool == "browser_navigate":
        st["navigations"] += 1
        st["domains"][domain] += 1


def registry_proposal() -> dict[str, Any]:
    """The exact kwargs a human (normally via the Foreman, see deploy/playwright-mcp/README.md) passes to
    ``aecom.tools.catalog.propose_tool`` to register the Playwright MCP server. Proposing it does not grant
    anyone access: it still needs ``sandbox_tool`` -> ``approve_tool`` -> ``grant_tool(..., agent_id="researcher")``
    (all human/T3 steps) before the Researcher can load it, and it stays off until a human runs
    ``docker compose up`` for deploy/playwright-mcp/docker-compose.yml in the first place."""
    return {
        "name": "playwright",
        "kind": "mcp_server",
        "description": ("Read-only headless browser (Playwright MCP) for JavaScript-heavy public pages, run in an "
                        "isolated container with no host/LAN access and a fresh profile per session. Researcher "
                        "only; navigate/read/screenshot allowed, click/type/submit/login/download denied by policy "
                        "and by this module. See docs/wiki/Browser-Access.md."),
        "launch_config": {
            "transport": "stdio",
            "command": "docker",
            "args": ["compose", "-f", "deploy/playwright-mcp/docker-compose.yml", "run", "--rm", "--no-deps",
                     "playwright-mcp"],
            "read_only_tools": sorted(READ_ONLY_TOOLS),
        },
    }
