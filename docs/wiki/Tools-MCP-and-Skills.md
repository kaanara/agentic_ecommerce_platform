# Tools, MCP and Skills

## Built-in tool catalog
Defined in `src/aecom/tools/catalog.py` with `@tool(domain, side_effect, terminal)`. Tiers and agent restrictions come from `policy.yaml`.

| Tool | Domain | Tier | Agents | Purpose |
|---|---|---|---|---|
| `list_products` | store | T0 | any | List catalog products by status |
| `get_inventory` | store | T0 | any | Per-SKU availability / unassigned keys |
| `list_orders` / `get_order` | store | T0 | any | Orders (incl. customer email) |
| `create_product` | store | T1 | store_ops | Create a draft product (VAT-inclusive EUR) |
| `activate_product` | store | T1 | store_ops | Make purchasable |
| `update_price` | store | T2 | store_ops, finance | Change price (> 20% escalates) |
| `add_license_keys` | store | T1 | store_ops, supply | Add or mint licence keys |
| `deliver_order` | store | T1 | store_ops | Assign key/link + email (idempotent) |
| `refund_order` | store | T2 | support, finance | Refund, revoke key (> €50 escalates) |
| `send_email` | comms | T2 | support, store_ops, marketing | One email via AgentMail; only to the customer of the current case, anyone else escalates |
| `reply_email` | comms | T1 | support | Reply in the thread of the inbound email being handled (refuses other customers' emails) |
| `send_newsletter` | comms | T3 | marketing | Bulk email to a segment |
| `list_inbox` | comms | T0 | any | Inbound emails (fenced), with `message_id` for replies |
| `create_campaign` | marketing | T1 | marketing | Draft campaign |
| `launch_campaign` | marketing | T2 | marketing | Go live (> €150 or over budget escalates) |
| `pause_campaign` | marketing | T1 | marketing, finance, ceo | Pause |
| `list_campaigns` | marketing | T0 | any | Spend, conversions, ROAS |
| `publish_social_post` | marketing | T3 | marketing | Brand social post |
| `get_pnl` / `get_kpis` | finance | T0 | any | Ledger P&L / KPIs |
| `record_expense` | finance | T2 | finance | Book expense (> €200 escalates) |
| `list_vendors` | supply | T0 | any | Vendors & subscriptions |
| `register_vendor` | supply | T2 | supply, foreman | New vendor (> €50/mo escalates) |
| `list_okrs` / `set_okr` | exec | T0 / T1 | any / ceo | OKRs |
| `list_decisions` / `record_decision` | exec | T0 / T1 | any / ceo | ADRs (also written to `docs/decisions/`) |
| `request_provisioning` | exec | T3 | ceo | Ask a human to create an account/credential |
| `read_company_doc` | exec | T0 | any | GOALS / BRAND |
| `list_pending_approvals` | exec | T0 | any | HITL queue |
| `fetch_url` | research | T0 | any | Web page as fenced text |
| `remember` / `recall_memory` | memory | T1 / T0 | any | Long-term memory (own + shared) |
| `propose_lesson` | memory | T1 | any | Procedural memory proposal |
| `list_registry` | registry | T0 | any | Tools/skills/MCP + grants |
| `propose_tool` | registry | T1 | foreman, researcher | Propose MCP server or skill |
| `sandbox_tool` | registry | T1 | foreman | Move to sandbox |
| `approve_tool` / `grant_tool` | registry | T3 | foreman | Promote / grant (human) |
| `submit_plan` / `submit_verdict` / `submit_brief` | control | T0 | foreman / judge / any | Terminal structured outputs |

### Adding a built-in tool
1. Add a typed, documented function with `@tool("domain", side_effect=True|False)` in `catalog.py`. The docstring becomes the model-facing description.
2. Add a rule in `policy.yaml`. Without one, a side-effecting tool is T3 by default.
3. Add it to the relevant agents' `TOOLS.md`. `uv run aecom init` syncs it into existing databases too: missing `TOOLS.md` tools are added to the allowlist and audited as `allowlist_sync`. The sync only adds. Removing a tool from `TOOLS.md` doesn't revoke it; init logs it as an extra grant, and you revoke it explicitly with `aecom registry revoke AGENT TOOL --by NAME [--note ...]` (`aecom registry list [--agent ID]` shows the grants). Revoking is human-only (CLI, audited as `allowlist_revoke`; agents have no tool for it). If the agent's `TOOLS.md` still lists the tool, the next `aecom init` (which every `aecom` command runs first) re-adds it, so remove it from `TOOLS.md` too (the command warns).
4. Add a policy test case in `tests/test_core.py`.

> **Naming gotchas:** don't name parameters `config` (LangChain reserves it for `RunnableConfig`) or `copy`/`json`/`schema` (these shadow pydantic `BaseModel` attributes).

## Registry lifecycle (self-management)
```
propose_tool (T1) → proposed → sandbox_tool (T1) → sandboxed → approve_tool (T3, human) → approved → grant_tool (T3, human) → usable by agent
```
| Status | Who can use it | Gate |
|---|---|---|
| proposed | nobody | — |
| sandboxed | Foreman only | every side-effecting call ≥ T3 |
| approved + granted | allowlisted agents | normal rules; unclassified side-effect tools = T3 |
| deprecated | nobody | — |

### MCP server entries
`propose_tool(kind="mcp_server", launch_config=...)`:
```json
{"transport": "stdio", "command": "uvx", "args": ["mcp-server-fetch"], "read_only_tools": ["fetch"]}
```
or `{"transport": "streamable_http", "url": "https://…/mcp"}`. Tools listed in `read_only_tools` are treated as T0. Everything else falls under its policy rule (key `mcp:<server>/<tool>`), or T3 if there's no rule.

#### Policy rules for MCP tools (F5)
`policy.yaml → rules` accepts two MCP-specific key shapes, with the same fields as any other rule (`tier`, `agents`,
`limits`, `price_change_pct_max`, `monthly_budget_setting`, `reason`, …):
- `mcp:<server>/<tool>` — an exact rule for one tool on one server, e.g. `mcp:aecom-store/refund_order`.
- `mcp:<server>/*` — every other tool on that server that doesn't have its own exact rule.

`aecom.policy.gate.resolve_rule` picks, in order: the exact rule, then the server wildcard, then
`defaults.mcp_unclassified_tier` (ships as `T3` — an MCP tool nobody classified always needs a human; a read-only
call with no rule at all still stays T0, same as an unclassified builtin tool). On top of that, an *unclassified*
MCP tool (no exact or wildcard rule matched) whose name looks destructive or financial — `create_refund`, `delete`,
`send`, `pay`, `transfer`, `charge`, `submit`, `purchase`, `login`, `sign_in`, `download` as a substring,
case-insensitive (`aecom.policy.gate.looks_destructive`) — is bumped to at least T3 even if a vendor marked it
read-only, and the escalation is audited like any other non-T0 decision. A `T4` MCP rule is a hard denial, exactly
like a built-in `T4` rule. A sandboxed server still forces at least `defaults.sandboxed_tool_tier` on every
side-effecting call, whatever its rule says — sandboxed servers stay read-only, unchanged by F5.

Only the servers this repo actually seeds get a *live* rule (`src/aecom/bootstrap.py` seeds `aecom-store`); see
[Policy and Approvals](Policy-and-Approvals#mcp-tools-f5) for the live examples and a commented-out one for a vendor
we don't run (Stripe). `aecom.policy.versions.validate_yaml` rejects a `T0`/`T1` wildcard on a server whose name
looks financial (`stripe`, `paypal`, `pay`, `bank`, `billing`, `invoice`, `checkout`, `finance`, `wallet`, `ledger`)
unless the rule itself carries `acknowledge_financial_wildcard: true` — a real field, not a YAML comment, since
comments don't survive parsing and can't be checked. `loosens()` treats lowering an MCP rule's tier/limits, or
lowering `defaults.mcp_unclassified_tier`, exactly like any other loosening (step-up required).

### Vendor MCP onboarding: read-only tools first
A new vendor's MCP server reaches an agent in two steps. The agent gets the read-only tools first, and a human widens that later.
```
propose_tool (T1) → sandbox_tool (T1) → approve_tool (T3, human) → grant_tool level="read_only" (T3, human)
                  → later: grant_tool level="full" (T3, human)
```
- When a vendor finishes provisioning, `provisioning.py` hands the Foreman an integration task. The Foreman calls `propose_tool(kind="mcp_server", ...)` and sandboxes the server, so you end up with a proposed and then sandboxed registry entry. Until you approve it, only the Foreman can load it, and every side-effecting call escalates.
- `grant_tool(tool_name, agent_id)` defaults to `level="read_only"`. The agent loads **only** the tools in `launch_config.read_only_tools` (T0). The server's other tools aren't loaded at all, so the model never sees them. If an approval for one of them gets parked anyway, it's refused when executed.
- `grant_tool(..., level="full")` is the upgrade. Like every `grant_tool` call it is T3, so a human approves the exact level. With a full grant all of the server's tools load, but they're still gated: a side-effecting tool without a `mcp:<server>/<tool>` policy rule stays T3, and `forbidden_patterns` still deny sign-up, login and form tools (T4).
- The level is stored in the registry entry's config as `config["grants"][agent_id] = "read_only" | "full"`. A grant without a recorded level, including one that predates levels, counts as read-only. Only `grant_tool` writes levels: `propose_tool` drops any `grants` key from the agent-supplied `launch_config`. If the server is re-proposed (a new version), its config is replaced and the grants go back to read-only.

Loading is done by `registry/mcp_manager.py` through `MultiServerMCPClient`, with a timeout (`MCP_LOAD_TIMEOUT_S`). It can be switched off with `MCP_AUTOLOAD=false`. A server that fails to load is audited and skipped; the agent still runs.

### Skills
`propose_tool(kind="skill", launch_config={"instructions": "..."})`. Approved and granted skills are appended to the agent's system prompt as `## Skill: <name>`. Use skills for reusable procedures like EU VAT pricing or a refund reply template.

Two ways a skill's text ends up in the registry today:
- **An agent proposes it** (normally the Foreman or the researcher — see `agents/foreman/TOOLS.md` /
  `agents/researcher/TOOLS.md`): `propose_tool` → `sandbox_tool` → human `approve_tool` → human `grant_tool`, exactly
  like the "self-tooling" scenario's `skill-eu-vat-pricing` (`src/aecom/scenarios.py`). This always lands at
  `status="proposed"`, whatever is passed in.
- **A human-reviewed doc is seeded at `aecom init`** (`src/aecom/bootstrap.py`: `seed_runbook_skills`,
  `seed_scraping_policy_skill`). These are this app's own *operational* procedures (incident runbooks, the
  researcher's scraping policy): the doc itself goes through normal PR review, so the seed writes
  `status="approved", approved_by="system"` directly and only still needs a `grant_tool` to reach an agent.

### Platform skills (F6): vendor/compliance checklists as files, proposed not auto-approved
`skills/platform/*.md` (Etsy digital-listing rules, Shopify product-page SEO, EU VAT/OSS) are the F6 roadmap item.
Each file carries YAML frontmatter in the same style as `agents/*/TOOLS.md` (`name`, `kind: skill`,
`status: proposed`, `candidate_agents`, `version`, `last_reviewed`) with the skill's instructions as the markdown
body, plus a `Sources to verify against` list (official domains only) and a `Last reviewed` line.

These are vendor policy / tax-compliance content, not this app's own reviewed operational procedures, so they
deliberately use the **first** path above, not the `seed_*_skill` "system-approved" shortcut: nothing auto-promotes
them past `proposed`. To actually grant one, a human (normally via the Foreman) reads the file and calls, verbatim:
```python
propose_tool(name, "skill", description, {"instructions": body})  # body = the file's markdown, after frontmatter
sandbox_tool(name)
approve_tool(name)      # human
grant_tool(name, agent_id)  # human, per candidate_agents in the frontmatter
```
`tests/test_platform_skills.py` proves each file's parsed body is valid input to this exact, unmodified call chain,
and that `skills_for(agent)` only returns the text after all four steps — `propose_tool`/`sandbox_tool` alone never
expose it to any agent.

There is no code yet that reads `skills/platform/*.md` automatically at `aecom init` (unlike `Runbooks.md`). If that
becomes desirable, the shape would be a new `seed_platform_skill_files()` in `bootstrap.py`, parallel to
`seed_runbook_skills`, but — intentionally — stopping at `propose_tool`/`sandbox_tool` instead of writing
`status="approved"` directly, so a human still approves and grants each one:
```python
def seed_platform_skill_files(path: Path = ROOT / "skills" / "platform") -> list[str]:
    """F6: propose+sandbox each skills/platform/*.md file; a human still approves and grants it."""
    from aecom.context import current_agent
    from aecom.persona.loader import split_frontmatter
    from aecom.tools.catalog import propose_tool, sandbox_tool

    proposed: list[str] = []
    token = current_agent.set("system")
    try:
        for file in sorted(path.glob("*.md")):
            fm, body = split_frontmatter(file.read_text())
            with session() as s:
                if s.get(RegistryTool, fm["name"]) is not None:
                    continue  # already proposed/sandboxed/approved: don't reset a human's progress
            propose_tool(fm["name"], "skill", fm["name"], {"instructions": body})
            sandbox_tool(fm["name"])
            proposed.append(fm["name"])
    finally:
        current_agent.reset(token)
    return proposed
```
called from `init()` alongside `seed_runbook_skills()`/`seed_scraping_policy_skill()`. This repo's catalog/registry
code is intentionally untouched by this change; the above is a patch to review, not something already applied.

## The company's own MCP servers
```bash
uv run aecom mcp store        # list_products, get_inventory, list_orders, get_order, create/activate/update/deliver/refund…
uv run aecom mcp marketing    # campaigns, social, email
uv run aecom mcp finance      # P&L, KPIs, vendors, OKRs, decisions
uv run aecom mcp registry     # registry lifecycle
uv run aecom mcp all
```
External clients act as agent `mcp_client`. Rules with an `agents:` list deny it, so in practice it gets the T0 and unrestricted T1 tools. Example for Claude Desktop / Claude Code:
```json
{"mcpServers": {"aecom-store": {"command": "uv", "args": ["--directory", "/path/to/repo", "run", "aecom", "mcp", "store"]}}}
```
