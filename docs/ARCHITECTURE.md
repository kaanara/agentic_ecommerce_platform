# Architecture

```
                         ┌──────────────── Human board ────────────────┐
                         │ Approvals dashboard · API · CLI · kill switch │
                         └───────────────▲───────────────┬──────────────┘
                                         │ ops_approvals │ approve → executes parked call verbatim
 triggers ─┐                             │               ▼
 scheduler │   ┌──────────── Company graph (LangGraph) ────────────────────────────┐
 events  ──┼──▶│ CEO (Opus) ─▶ Foreman (Sonnet) ─▶ departments in parallel waves ─▶ │
 API/CLI ──┘   │   ▲                     Store Ops · Marketing · Support (Haiku)    │
               │   │                     Finance (Sonnet) · Supply (Haiku)          │
               │   └── CEO close ◀── Judge (Sonnet) ◀── (veto → rework once) ◀─────┘
               │ Selection graph: Researcher → Finance → Judge → CEO → ADR + provisioning
               └──────────────────────────────┬────────────────────────────────────┘
                                              │ every tool call
                                   ┌──────────▼──────────┐
                                   │ Policy gate (T0–T4) │  policy.yaml · allowlists · budgets · kill switch
                                   └──────────┬──────────┘
          ┌───────────────┬───────────────────┼─────────────────┬──────────────────────┐
     StoreAdapter     AgentMail         Ledger / KPIs     Registry (tools,     MCP servers (approved /
   (mock → platform)  (email)          (double-entry)     skills, MCP)         sandboxed, via adapters)
          └───────────────┴──────── Event bus (ops_events) ◀─ reflexes (delivery, restock) ─┘
                                   Database: SQLite (default) | Postgres + pgvector
```

## Components and contracts

| Component | File | Contract |
|---|---|---|
| Persona | `agents/<id>/*.md`, `persona/` | IDENTITY (YAML: id, model_tier, reports_to), SOUL and AGENTS go into the prompt, TOOLS (allowlist seed), POLICY (budget) |
| Agent runner | `graph/agents.py` | `run_agent(agent_id, task) → AgentResult`. Checks the budget, builds gated tools, runs the loop, records cost to the ledger |
| Company graph | `graph/supervisor.py` | `run_company(directive)`. State is checkpointed per `run_id` |
| Selection graph | `graph/selection.py` | `run_selection(category, need, candidates)` produces an ADR and a provisioning request |
| Policy gate | `policy/gate.py`, `policy.yaml` | `evaluate()` → allow, deny or escalate. Escalations are parked in `ops_approvals` |
| Approvals | `approvals.py` | `approve(id, by, note, args_override)` runs the exact parked call. `reject()` notifies via the bus |
| Registry | `registry/` | Lifecycle proposed → sandboxed → approved, plus allowlists. Approved skills are added to prompts |
| Store | `store/adapter.py` | `StoreAdapter` protocol. The CEO's platform decision picks the adapter |
| Events | `bus/events.py`, `handlers.py` | Durable topics: `order.paid`, `inventory.low`, `inventory.out_of_stock`, `email.received`, `vendor.degraded`, `campaign.ended`, `approval.resolved` |
| MCP servers | `mcp_servers/server.py` | Same catalog and same gate. External clients act as `mcp_client` (read-only by policy) |

## Key design decisions
1. **Escalation runs the call later instead of pausing the graph.** A human approves the exact tool and arguments that will run. Graph runs never block waiting for people, and the approval record is the audit evidence.
2. **Reflexes vs. agents.** Routine event handling (delivery, restock) runs deterministically under an agent identity. It still goes through the gate, so it's audited and the kill switch applies. LLM agents handle anything that needs judgment.
3. **Humans provision, agents decide.** Agents never create accounts, accept terms, or handle credentials. The CEO raises provisioning requests, and secrets only reach tools via `SecretsProvider`.
4. **Self-management is a governed lifecycle.** Agents can propose and sandbox MCP servers and skills. Promotion and grants are T3 (human only).
5. **Cost is a first-class signal.** Every agent run books `ai_expense` in the ledger. Budgets are enforced per agent per day, and "AI cost per order" is an OKR.

## Moving beyond the PoC
- Real `StoreAdapter`s (Lemon Squeezy / Paddle / Shopify) plus webhooks into `order.paid`.
- Ad-platform and social MCP servers, granted to Marketing through the registry.
- A Redis/NATS event bus, pgvector embeddings for `memory_items`, Alembic migrations.
- "Harness" self-management: versioned prompts/graphs in the registry, with A/B runs scored by the Judge.
