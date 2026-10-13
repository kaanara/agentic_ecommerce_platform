# Architecture

```
 triggers ─┐   ┌──────────── Company graph (LangGraph) ────────────────────────────┐
 scheduler │   │ ceo_direct ─▶ foreman_plan ─▶ execute (parallel waves) ─▶ judge   │
 events  ──┼──▶│      ▲                                     │ rejected (once)      │
 API/CLI ──┘   │      └───────── ceo_close ◀── approved ────┴─▶ foreman_plan       │
               │ Selection graph: research ─▶ finance ─▶ judge ─▶ ceo_decide       │
               └───────────────────────────────┬──────────────────────────────────┘
                                               │ every tool call
                                    ┌──────────▼──────────┐
                                    │ Policy gate (T0–T4) │ policy.yaml · allowlists · budgets · kill switch
                                    └──────────┬──────────┘
   StoreAdapter · AgentMail · Ledger/KPIs · Registry · MCP servers ── Event bus (ops_events) ── reflexes
                        Database: SQLite (default) | Postgres + pgvector
```

## Layers
| Layer | Module | Responsibility |
|---|---|---|
| Personas | `agents/<id>/*.md`, `src/aecom/persona/` | Who each agent is and how it works. Loaded into an `AgentSpec` and a system prompt |
| Agent runner | `graph/agents.py` | One agent, one task: budget check, gated tools, model loop, cost accounting |
| Orchestration | `graph/supervisor.py`, `graph/selection.py` | LangGraph state machines, checkpointed per run |
| Governance | `policy/gate.py`, `policy/policy.yaml`, `approvals.py` | Allow, deny or escalate decisions; the human approval queue |
| Capabilities | `tools/catalog.py`, `registry/`, `mcp_servers/` | Built-in tools, the tool/skill/MCP registry, the company's own MCP servers |
| Domain | `store/`, `ledger.py`, `integrations/agentmail.py` | Storefront adapter, double-entry books, email |
| Events | `bus/events.py`, `handlers.py`, `scheduler.py`, `simulator.py` | Durable event bus, reflexes, time triggers, market simulation |
| Interfaces | `cli.py`, `api/app.py`, `dashboard/` | CLI, REST API, Streamlit dashboards |

## The company graph
State (`CompanyState`): `run_id`, `directive`, `ceo_brief`, `plan`, `results` (append), `verdict`, `iterations`, `summary`, `escalations` (append).

1. **ceo_direct**: the CEO grounds the directive in OKRs, KPIs and decisions, then writes a brief of at most 250 words.
2. **foreman_plan**: the Foreman calls `submit_plan` with 1–6 tasks `{id, agent, instruction, depends_on}`. Tasks for unknown agents are dropped and the drop is audited.
3. **execute**: tasks are grouped into dependency **waves**. Each wave runs concurrently with `asyncio.gather`. A task receives its dependencies' outputs as context.
4. **judge**: scores the latest results against the rubric and calls `submit_verdict`.
5. If the Judge rejects and there's no rework yet, the run goes back to the Foreman with feedback (`MAX_REWORK = 1`). Otherwise **ceo_close** writes the executive summary.

The checkpointer is `AsyncSqliteSaver` (`data/aecom.checkpoints.db`), or `AsyncPostgresSaver` on Postgres. `thread_id` is the `run_id`.

## The agent loop (`run_agent`)
1. Refuse the run if the agent's LLM spend today is at or above its budget (`POLICY.md` → `budget_eur_per_day`).
2. Build the system prompt: identity, SOUL, AGENTS, applied lessons, company goals, non-negotiable rules, and any granted skills.
3. Build the tools: the built-ins on the agent's allowlist and any granted or sandboxed MCP tools, each wrapped by the gate.
4. Loop for up to `AGENT_MAX_STEPS` steps: model → tool calls → `ToolMessage`s. The loop stops on plain text or a **terminal tool** (`submit_plan`, `submit_verdict`, `submit_brief`).
5. Record an `ops_agent_runs` row (tokens, cost, status) and book the cost as `ai_expense` in the ledger.

The persona prompt is sent with Anthropic prompt caching (`cache_control: ephemeral`).

## Design decisions
| Decision | Why |
|---|---|
| Escalation parks the call instead of pausing the graph | The human approves exactly what will run. Runs never block on people, and the approval row is the audit evidence |
| Reflexes for routine events | Delivering a licence key doesn't need an LLM. Reflexes still pass the gate, so they're audited and the kill switch stops them |
| Terminal "submit_*" tools for structured output | Works the same with real Claude and with the scripted model; no JSON parsing of free text |
| Domain-prefixed tables instead of Postgres schemas | The same model runs on SQLite and Postgres |
| Our own agent loop instead of a prebuilt ReAct agent | Budget, cost and terminal-tool handling are explicit and easy to change |
| Scripted LLM mode | Deterministic tests and demos with no key and no spend |
