# aecom: an agentic e-commerce company for digital products (PoC)

A multi-agent system built on **LangGraph** and **Claude** that runs an e-products business end to end. It selects its own platform and ad channels, launches products, runs campaigns, delivers licence keys, handles support and refunds, controls finances, and manages its own MCP servers and skills. A policy gate and human approvals govern all of it.

| Agent | Model | Role |
|---|---|---|
| 🧭 CEO *Aurelia* | Opus | Strategy, OKRs, platform/tech decisions, final authority |
| 🔎 Researcher *Sage* | Sonnet | Market and vendor intelligence (web search) |
| ⚖️ Judge *Themis* | Sonnet | QA and compliance rubric, veto |
| 🛠️ Foreman *Atlas* | Sonnet | Plans, dispatches, manages tools/MCP lifecycle |
| 📊 Finance *Ledger* | Sonnet | P&L, pricing, ROI, budgets |
| 🏪 Store Ops *Quill* | Haiku | Catalogue, licence inventory, delivery |
| 📣 Marketing *Nova* | Haiku | Campaigns, social, newsletters |
| 💬 Support *Iris* | Haiku | Customer email (AgentMail), refunds |
| 🔗 Supply *Relay* | Haiku | Vendors, key pools, provisioning continuity |

Each agent lives in `agents/<id>/` as `IDENTITY.md` (metadata), `SOUL.md` (personality), `AGENTS.md` (procedures), `TOOLS.md` (allowlist) and `POLICY.md` (budget/limits). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and the [wiki](docs/wiki/Home.md).

## Quick start (no API key needed)

```bash
uv sync
cp .env.example .env
uv run aecom init --demo                 # schema, registry, OKRs, demo catalogue, 30 days of history
uv run aecom scenario all --offline      # 8 end-to-end scenarios with scripted agents
uv run aecom dashboard                   # Business · Agents · Approvals
uv run pytest
```

## Live mode (Claude decides)

Put `ANTHROPIC_API_KEY` in `.env`, set `LLM_MODE=anthropic`, then:

```bash
uv run aecom scenario product-launch
uv run aecom select ecommerce_platform "Sell licences + courses in the EU, VAT-compliant" -c Shopify -c "Lemon Squeezy" -c Paddle
uv run aecom run "Find a second product line that raises LTV, and plan its launch within €300"
uv run aecom approvals                   # or use the Approvals dashboard
uv run aecom scheduler --demo            # a market day every minute + daily/weekly agent jobs
uv run aecom api                         # REST: /runs /approvals /kill-switch /webhooks/agentmail
```

## Scenarios

| Name | What it shows |
|---|---|
| `bootstrap` | Selection graph → CEO picks the storefront/MoR → ADR in `docs/decisions/` → provisioning request (human) |
| `product-launch` | CEO → Foreman plan → Finance price → Store Ops list + stock → Supply verify → Judge → CEO |
| `campaign` | Ad-platform selection, then a €300 campaign and social post, both escalated |
| `order-delivery` | `order.paid` → licence/download delivery by email → double-entry ledger |
| `support-refund` | Inbound email containing a prompt-injection attempt → €79 refund escalated, injection ignored |
| `supply-continuity` | Key pool drained → restock reflex. Vendor outage → fallback vendor escalated |
| `self-tooling` | Foreman proposes and sandboxes an MCP server and a skill. A human approves and grants them |
| `weekly-review` | Finance report → Marketing pauses ROAS < 1.5 → Judge proposes a lesson → CEO close |

## Governance
- **Escalation matrix:** `src/aecom/policy/policy.yaml` (T0 read → T4 forbidden). For example, refunds over €50, campaigns over €150 or beyond the monthly budget, price changes over 20%, every public post and newsletter, every tool promotion or grant, and every provisioning request go to a human.
- **Kill switch:** dashboard, `aecom kill-switch on`, or `POST /kill-switch?on=true`. This blocks every side-effecting action.
- **Secrets:** only in `.env`, never in prompts. Agents ask a human to provision accounts and never sign up themselves.
- **Email:** AgentMail. Messages stay in the local outbox unless `AGENTMAIL_LIVE=true`.

## MCP
The company exposes its own MCP servers (`uv run aecom mcp store|marketing|finance|registry|all`). They go through the same policy gate, so external clients get read-only access by default. Agents consume registry MCP servers through `langchain-mcp-adapters`.
