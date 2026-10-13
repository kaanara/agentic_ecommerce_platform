# Roadmap

## PoC: done
- 9 agents with SOUL / IDENTITY / AGENTS / TOOLS / POLICY files
- Company and selection graphs (LangGraph, checkpointed)
- Policy gate (T0–T4), HITL approvals with edit-before-approve, kill switch, budgets, append-only audit
- Tool / skill / MCP registry with a governed lifecycle; company MCP servers
- Mock store, double-entry ledger with EU VAT, AgentMail integration (outbox by default)
- Event bus, reflexes, scheduler, market simulator
- Business / Agents / Approvals dashboards; 8 scenarios with offline replay; test suite

## Next
1. **Live validation:** run every scenario against real Claude and record the outcomes and costs. Calibrate `PRICE_*`.
2. **Real storefront:** implement the adapter for the CEO's chosen platform, plus webhooks that publish `order.paid`.
3. **Real channels:** ad-platform and social MCP servers granted to Marketing; spend reconciliation into the ledger.
4. **Postgres by default:** Alembic migrations, pgvector embeddings for `memory_items`, a cross-database fix for the dashboard SQL.
5. **Event bus:** Redis Streams or NATS; idempotent consumers; dead-letter queue.
6. **Lessons loop:** apply Judge- and human-approved lessons to `AGENTS.md` automatically, as a versioned PR.
7. **Harness self-management:** versioned prompts, graphs and model tiers in the registry; A/B runs scored by the Judge; human-approved promotion.
8. **Security:** vault-backed `SecretsProvider`, per-tool rate limits, red-team tests for prompt injection, PII redaction in logs.
