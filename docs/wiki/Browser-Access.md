# Browser Access (M2): optional, read-only Playwright MCP

[Roadmap M2](Roadmap#roadmap) — Playwright MCP through the registry, for JavaScript-heavy public pages Researcher's
`fetch_url` (plain HTTP GET + tag-stripping) can't render. **Off by default**, Researcher only, read-only. Code:
`src/aecom/browser_guard.py` (the guard), `src/aecom/registry/mcp_manager.py` (wiring, server name `"playwright"`
only), `deploy/playwright-mcp/` (the container a human runs). Tests: `tests/test_browser_mcp.py`.

## Why a separate guard module, on top of the policy gate

The gate (`aecom.policy.gate`, F5) already tiers `mcp:playwright/<tool>` calls from YAML — see
[Policy block](#policy-patch) below. That's enough to make the interactive tools (`browser_click`, `browser_type`,
...) a hard `T4` deny and the read-only ones (`browser_navigate`, `browser_snapshot`, `browser_take_screenshot`)
`T1` allow. What YAML tiers *can't* express is "not **this** URL" or "not this many calls" — a policy rule has no
concept of the argument a specific call carries beyond the T2 `limits`/`price_change_pct_max` checks, which don't
apply to a T1 rule. `aecom.browser_guard` is the code-level layer that fills that gap, wired into
`aecom.registry.mcp_manager.load_mcp_tools`/`call_mcp_tool` for the server named exactly `"playwright"` and no
other server:

- **`check_navigation(url)`** — delegates to `aecom.net_guard.resolve_public`, so it rejects:
  - any scheme other than `http`/`https` (`file:`, `javascript:`, `data:`, `chrome:`, `blob:`, ...);
  - a URL carrying credentials (`http://user:pass@host/`);
  - a host or literal IP that resolves to loopback, RFC1918-private, link-local (including the
    `169.254.169.254` cloud-metadata address), multicast, reserved or unspecified — including a hostname that
    only *resolves* to one of those, including the deprecated/tunnelled IPv6 forms `net_guard` already catches
    (IPv4-mapped, 6to4, Teredo, NAT64, IPv4-compatible, SIIT). This is also what keeps the browser off the Pi's own
    LAN and the Pi itself, on any port: every address a Pi (or anything else on the LAN) answers on is private or
    loopback, so there's no "allow this port, block that one" exception to carve out.
- **`check_tool_call(tool, args)`** — deny-by-default: anything not in `READ_ONLY_TOOLS` (`browser_navigate`,
  `browser_snapshot`, `browser_take_screenshot`, `browser_navigate_back`, `browser_wait_for`) is refused, whether
  or not it's one of the interactive tools M2 explicitly names (a future, unlisted Playwright tool is denied too –
  this is an allowlist, not a denylist). For `browser_navigate`, also calls `check_navigation` and enforces three
  budgets tracked per `aecom.context.current_run`: a per-run call budget, a per-run navigation budget and a
  per-run-per-domain navigation cap. A denied/over-budget call is never counted, so retrying it doesn't burn
  budget twice.
- Both raise `BrowserGuardError` (a `PermissionError`) with a short, model-facing reason.

## Wiring (mcp_manager.py, `"playwright"` only)

`load_mcp_tools` wraps each loaded Playwright tool with `_wrap_playwright_tool` **before** `gate_external` sees it
— every other server's tools are untouched. The wrapper:
1. runs `check_tool_call` before the real Playwright tool is invoked; a denial returns
   `"DENIED by browser guard: <reason>"` without the call ever reaching the container;
2. on an allowed call, fences the result with `aecom.tools.catalog.fence("mcp:playwright/<tool>", result)` —
   exactly the treatment `fetch_url` gives a fetched page — which both wraps it as `<untrusted source="...">...
   </untrusted>` (so the model treats it as data, never instructions) and marks the run tainted (I15), via
   `fence`'s own call to `mark_tainted()`.

`call_mcp_tool` (the human-approval re-invocation path) applies the identical guard + fence for `server ==
"playwright"`; in practice this path is rarely reached for this server since the only rules below are `T1`
(allow, never escalates) or `T4` (deny, never escalates) — there's no `T2`/`T3` Playwright rule to approve.

Every other MCP server in the registry (`aecom-store`, any future vendor server) takes the original, un-wrapped
path: `_wrap_playwright_tool`/`browser_guard` only ever run for `server == "playwright"`.

## Lifecycle: proposed → sandboxed → approved → granted (human, every step after the first)

See `deploy/playwright-mcp/README.md` for the full operational walk-through — container hardening, egress note,
and the exact `propose_tool` call. Summary:

```python
from aecom.browser_guard import registry_proposal
from aecom.tools.catalog import propose_tool, sandbox_tool, approve_tool, grant_tool

propose_tool(**registry_proposal())     # T1 — the Researcher may call this itself (agents/researcher/TOOLS.md)
sandbox_tool("playwright")              # T1 — Foreman only; not in researcher/TOOLS.md
approve_tool("playwright")              # T3 — human
grant_tool("playwright", "researcher")  # T3 — human; level="read_only" (default) loads only read_only_tools
```

The container (`deploy/playwright-mcp/docker-compose.yml`) is a second, independent off switch: a human has to
`docker compose up` it (or let the `stdio` launch command in `registry_proposal()` start it per call) *and* a
human has to finish the lifecycle above — either alone leaves the Researcher with no usable browser tools.

## Policy patch

Add to `src/aecom/policy/policy.yaml → rules` (the gate's `limits` field on a `T1` rule is not evaluated by
`aecom.policy.gate.evaluate` — the gate only checks `limits` for a `T2` rule — so the ones below document the
budgets `aecom.browser_guard` enforces in code; they are not a second, redundant enforcement point):

```yaml
rules:
  # ---------------------------------------------------------------- M2: Playwright MCP (read-only browser)
  mcp:playwright/browser_navigate:
    tier: T1
    agents: [researcher]
    limits: {max_calls_per_run: 20, max_navigations_per_run: 5, max_calls_per_domain_per_run: 10}
    reason: "M2: read-only navigation; budgets enforced by aecom.browser_guard.check_tool_call, not this rule."
  mcp:playwright/browser_snapshot:
    tier: T1
    agents: [researcher]
    limits: {max_calls_per_run: 20}
    reason: "M2: read-only accessibility snapshot of the current page."
  mcp:playwright/browser_take_screenshot:
    tier: T1
    agents: [researcher]
    limits: {max_calls_per_run: 20}
    reason: "M2: read-only screenshot of the current page."
  # Explicit T4 denial for every interactive/destructive Playwright tool. Several of these already match
  # defaults.forbidden_patterns (*browser_click*, *browser_type*, *file_upload*), which denies them regardless of
  # tier; the rules below cover the rest and make every one of them explicit and independent of that pattern list.
  mcp:playwright/browser_click: {tier: T4}
  mcp:playwright/browser_type: {tier: T4}
  mcp:playwright/browser_fill_form: {tier: T4}
  mcp:playwright/browser_press_key: {tier: T4}
  mcp:playwright/browser_select_option: {tier: T4}
  mcp:playwright/browser_file_upload: {tier: T4}
  mcp:playwright/browser_evaluate: {tier: T4}
  mcp:playwright/browser_run_code: {tier: T4}
  mcp:playwright/browser_install: {tier: T4}
  mcp:playwright/browser_drag: {tier: T4}
  mcp:playwright/browser_hover: {tier: T4}
  mcp:playwright/browser_handle_dialog: {tier: T4}
  mcp:playwright/browser_tabs: {tier: T4}
  # Wildcard: anything else on this server (a future/renamed Playwright tool) is denied outright, not escalated —
  # a T4 wildcard means no amount of retrying ever reaches an approval prompt a human could rubber-stamp by habit.
  # This also means `defaults.mcp_unclassified_tier` is never consulted for this server.
  mcp:playwright/*: {tier: T4}
```

`agents: [researcher]` on the three `T1` rules means every other agent is denied (`DENIED by policy (T2…T4):
mcp:playwright/browser_navigate is reserved for researcher.` — well, `T1` here, same message shape); the `T4`
rules deny everyone, including the Researcher. `mcp:playwright/*: {tier: T4}` does **not** need
`acknowledge_financial_wildcard` (that flag only guards a `T0`/`T1` wildcard on a financial-looking server name;
`T4` is already the strictest tier and "playwright" doesn't match the financial-name check anyway).

## Config rows

| Variable | Type | Default | Notes |
|---|---|---|---|
| `browser_max_calls` | int | 20 | Per-run cap on every Playwright MCP tool call (read via `getattr`, so it degrades to the built-in default on an older `Settings` that doesn't define it yet) |
| `browser_max_navigations` | int | 5 | Per-run cap on `browser_navigate` calls specifically |
| `browser_max_calls_per_domain` | int | 10 | Per-run, per-domain cap on `browser_navigate` calls to the same host |

None of these three fields exist in `src/aecom/config.py` yet (that file is out of scope for this change); add them
as ordinary `Settings` fields (see `Configuration.md` for the live-editable-settings convention) once a human wants
them tunable from the dashboard instead of the built-in defaults:

```python
# src/aecom/config.py: Settings, near mcp_autoload/mcp_load_timeout_s
browser_max_calls: int = 20                 # M2: aecom.browser_guard per-run call budget for mcp:playwright/*
browser_max_navigations: int = 5            # M2: aecom.browser_guard per-run browser_navigate budget
browser_max_calls_per_domain: int = 10      # M2: aecom.browser_guard per-run, per-domain browser_navigate cap
```

## Rate limits and fencing, in short

- Budgets are tracked **in-process, per run id** (`aecom.context.current_run`), not in the database — a denied
  call costs nothing, so retrying after a `DENIED` message doesn't make things worse.
- Every allowed call's result is fenced as `<untrusted source="mcp:playwright/<tool>">...</untrusted>` and marks
  the run tainted — the Researcher (and anything downstream that reads its output, e.g. `submit_brief`,
  `remember`) must treat page content as data, never instructions, exactly like any other web page or email.
- `aecom.browser_guard.reset(run_id=None)` clears counters (used by tests; production run ids are never reused,
  so counters never need clearing there).

## See also
[Roadmap](Roadmap) (M2 entry) · [Policy and Approvals](Policy-and-Approvals#mcp-tools-f5) (F5 resolution order,
forbidden patterns) · [Tools, MCP and Skills](Tools-MCP-and-Skills#registry-lifecycle-self-tooling) (registry
lifecycle, grant levels) · `agents/researcher/TOOLS.md` (the Researcher's `propose_tool` grant).
