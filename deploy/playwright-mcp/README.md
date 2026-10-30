# Playwright MCP — optional, read-only browser (M2)

This directory holds the container for the Roadmap's M2 item: an optional, read-only headless browser
([Playwright MCP](https://github.com/microsoft/playwright-mcp)) the Researcher can use for JavaScript-heavy public
pages that `fetch_url`'s plain HTTP GET can't render. It is **off by default** — see
[docs/wiki/Browser-Access.md](../../docs/wiki/Browser-Access.md) for the full design (policy tiers, rate limits,
what's allowed/denied and why). This README is the operational "how do I actually turn this on" half.

## It stays off until a human says so, twice

Two independent switches both have to be flipped; either one alone keeps the browser unreachable by any agent:

1. **The container has to exist.** Nothing runs `docker compose up` automatically. A human runs it on the host
   that will run the browser (normally the same Pi, or a sidecar Docker host if you'd rather keep Chromium off the
   Pi's limited RAM/CPU — see `docker-compose.yml`'s resource limits).
2. **The registry entry has to be approved and granted.** Even with the container running, no agent can load it
   until a human completes the full lifecycle below. `mcp_autoload` being on and the container running together
   are not enough — `aecom.registry.mcp_manager.load_mcp_tools` only loads tools for a server that is `approved`
   (for a granted agent) or `sandboxed` (Foreman only), and only the tools in `read_only_tools` reach an agent on
   a `read_only` grant (the default).

## How the Foreman proposes it

The Researcher may *propose* the server (it's in `agents/researcher/TOOLS.md`) but, like every agent, can't
approve or grant it — those are human/T3 steps. In practice the Foreman (or a human directly, via the CLI/API)
calls `propose_tool` with exactly the arguments `aecom.browser_guard.registry_proposal()` returns, so the
`launch_config` a human reviews is the same one a human later points `docker compose` at:

```python
from aecom.browser_guard import registry_proposal
from aecom.tools.catalog import propose_tool

propose_tool(**registry_proposal())
# {"name": "playwright", "kind": "mcp_server", "description": "...",
#  "launch_config": {"transport": "stdio", "command": "docker",
#                     "args": ["compose", "-f", "deploy/playwright-mcp/docker-compose.yml", "run", "--rm",
#                              "--no-deps", "playwright-mcp"],
#                     "read_only_tools": ["browser_navigate", "browser_navigate_back", "browser_snapshot",
#                                          "browser_take_screenshot", "browser_wait_for"]}}
```

`command`/`args` here is a *stdio* MCP connection: `langchain-mcp-adapters` spawns `docker compose ... run --rm
playwright-mcp` itself (via `aecom.registry.mcp_manager._connection`) as the subprocess it talks MCP over stdin/
stdout with, once per agent tool call that needs it — each call gets a brand-new container (and, inside it,
`--isolated` gives it a brand-new in-memory browser profile too), not a long-lived daemon another call reuses. If
you'd rather run the server once and reuse it, swap `launch_config` for the `streamable_http` shape instead
(`{"transport": "streamable_http", "url": "http://..."}`) pointing at a container you keep running with
`docker compose up -d` and `--port`/`--host 0.0.0.0` added to `command:` — but then it's your job to make sure
nothing but this app can reach that port (no public port mapping) and that a session isn't accidentally shared
across unrelated agent runs.

## It stays "proposed" until a human finishes the chain

```python
propose_tool(**registry_proposal())   # proposed  — anyone can read this far in list_registry; nobody can use it
sandbox_tool("playwright")            # sandboxed — Foreman only, every side-effecting call still ≥ T3
approve_tool("playwright")            # approved  — human (T3); still nobody is granted
grant_tool("playwright", "researcher")  # granted, level="read_only" (the default) — human (T3)
```

Every step after the first is `T3`, i.e. always escalates to a human in the dashboard/API/CLI approvals queue —
`sandbox_tool`/`approve_tool`/`grant_tool` are not in `agents/researcher/TOOLS.md`, so the Researcher literally has
no tool call that could run them even if it wanted to. `grant_tool`'s default `level="read_only"` matters here more
than for most servers: it's what makes `read_only_tools` (`browser_navigate`, `browser_navigate_back`,
`browser_snapshot`, `browser_take_screenshot`, `browser_wait_for`) the *only* tools the Researcher's agent even
sees — Playwright's interactive tools (`browser_click`, `browser_type`, `browser_fill_form`, ...) are never loaded
into its toolbelt at all, on top of being denied by policy and by `aecom.browser_guard.check_tool_call` if they
somehow were. A `level="full"` grant is possible in principle (`grant_tool(..., level="full")`, also T3) but there
is no reason to make it for this server: the interactive tools stay denied by policy/`browser_guard` regardless,
so "full" only adds the *load* of tools that can never execute.

Granting any **other** agent than `researcher` works mechanically but is against the M2 design in
docs/wiki/Browser-Access.md (Researcher only) and the policy patch there restricts the three allowed tools to
`agents: [researcher]` — a human reviewing the grant should say no.

## What's actually in the container

See `docker-compose.yml` for the full, commented configuration. Summary:
- `mcr.microsoft.com/playwright/mcp`, pinned by tag (`PINNED-VERSION.md` explains pinning the digest too for a
  real deployment).
- `--headless --isolated`: no GUI, no saved profile — a fresh, in-memory browser context every container run, so
  there are never cookies or credentials to leak between sessions (or to a page at all: nothing ever logs in).
- `read_only: true`, `tmpfs: [/tmp]` only, `cap_drop: ALL`, `no-new-privileges`, non-root `user: node`: standard
  container hardening, in case the browser itself (Chromium) is exploited by a malicious page.
- No `network_mode: host`; a dedicated bridge network (`playwright-egress`), not the Pi's own network, not shared
  with any other compose project.
- No `ports:` (nothing needs to reach in), no secret `environment:`.
- CPU/memory limits, since an uncapped headless Chromium is an easy way to starve the Pi running everything else.

`docker-compose.yml` also explains, in a comment, why egress still needs a host-level iptables/nftables rule or an
egress proxy: Compose can express *which* network a container is on, not *which addresses* that network may reach,
so blocking the Pi's own LAN (RFC1918) from inside the browser container is a one-time host setup step, not
something this file can enforce by itself. `aecom.browser_guard.check_navigation` independently refuses to send
the browser to a private/loopback/link-local address for every navigation *this app* asks for — the two layers
cover different attackers (a malicious page vs. a compromised/malicious call site) and are meant to both be in
place, not to substitute for each other.
