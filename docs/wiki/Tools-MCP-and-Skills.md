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
3. Add it to the relevant agents' `TOOLS.md`. `uv run aecom init` syncs it into existing databases too: missing `TOOLS.md` tools are added to the allowlist and audited as `allowlist_sync`. The sync only adds. Removing a tool from `TOOLS.md` doesn't revoke it; init logs it as an extra grant, and you revoke it explicitly.
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

Loading is done by `registry/mcp_manager.py` through `MultiServerMCPClient`, with a timeout (`MCP_LOAD_TIMEOUT_S`). It can be switched off with `MCP_AUTOLOAD=false`. A server that fails to load is audited and skipped; the agent still runs.

### Skills
`propose_tool(kind="skill", launch_config={"instructions": "..."})`. Approved and granted skills are appended to the agent's system prompt as `## Skill: <name>`. Use skills for reusable procedures like EU VAT pricing or a refund reply template.

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
