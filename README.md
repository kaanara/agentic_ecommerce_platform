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
uv run aecom dashboard reset-password    # set the dashboard password (12+ characters); the login is on by default
uv run aecom dashboard                   # Overview · Chat · Approvals · Business · Agents · Costs · Reports · Trust · Health · Settings
uv run pytest
```

## Talk to the agents

- **Dashboard > Chat:** streamed replies, tools through the policy gate, attachments, agents can consult each other. Needs the dashboard login on.
- **Telegram:** pair a bot in Settings > Channels, then chat, approve or reject from your phone. WhatsApp is built but off by default; iMessage is not supported.
- **CLI:** `uv run aecom ask ceo "How are sales?"` or `uv run aecom chat finance`.
- **Agents write to you:** `message_owner` reaches Telegram, WhatsApp or ntfy, and always the dashboard inbox.
- **Everything is a setting:** keys, budgets, schedules, alert routing, policy rules and agent personas are edited in the dashboard (Settings); secrets go to `.env` only.

Stuck? See the [Help page](docs/wiki/Help.md). To run it as always-on services on a Raspberry Pi (start at boot, restart after a power cut), see [Raspberry Pi](docs/wiki/Raspberry-Pi.md).

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
