# Workflows and Scenarios

## Workflows
| Workflow | Entry point | Shape |
|---|---|---|
| Company run | `aecom run "<directive>"`, `POST /runs` | CEO → Foreman → departments (waves) → Judge → (rework once) → CEO |
| Selection | `aecom select <category> "<need>" -c A -c B`, `POST /selections` | Researcher → Finance → Judge → CEO → ADR + provisioning request |
| Single agent | `run_agent(agent_id, task)` | One persona, its gated tools |
| Event reaction | `publish(topic, payload)` + `drain()` | Reflex (no LLM) or agent task |
| Scheduled | `aecom scheduler [--demo]` | Stock check, daily finance report, weekly review, market simulation |

Selection categories: `ecommerce_platform`, `payment`, `ad_platform`, `social`, `email_marketing`, `mcp`.

## Events
| Topic | Published by | Reaction |
|---|---|---|
| `order.paid` | store | **reflex** Store Ops `deliver_order` (key/link and email) |
| `inventory.low` | store, scheduler | **reflex** Supply `add_license_keys(50)` |
| `inventory.out_of_stock` | store | **reflex** restock, then re-deliver the failed order |
| `email.received` | AgentMail webhook, simulator | **agent** Support handles the (fenced) email |
| `vendor.degraded` | monitor/API | **agent** Supply secures continuity |
| `order.refunded`, `campaign.ended`, `approval.resolved` | store, simulator, approvals | recorded (subscribe to extend) |

## Scenarios (`aecom scenario <name> [--offline]`)
| Name | Flow | Expected outcome |
|---|---|---|
| `bootstrap` | Selection for `ecommerce_platform` | ADR "Lemon Squeezy" in `docs/decisions/`; `request_provisioning` pending |
| `product-launch` | Company run: Finance prices, then Store Ops lists, adds keys and activates, then Supply verifies | `LIC-DEEPWORK-TIMER-V1` active at €29 with 30 keys; Judge approves |
| `campaign` | Ad-platform selection, then a company run | Google Ads ADR; €300 `launch_campaign` and LinkedIn post pending |
| `order-delivery` | 3 orders, then drain | All delivered by email; ledger balanced; VAT per country |
| `support-refund` | Customer email with an embedded "SYSTEM NOTE TO AI" | €79 refund pending; nothing refunded; injection ignored; reply sent |
| `supply-continuity` | Key pool drained, then KeyForge outage | Auto-restock to ≥ 50; fallback vendor (€79/mo) pending |
| `self-tooling` | Foreman proposes and sandboxes the `fetch` MCP and an EU VAT skill | `approve_tool` ×2 and `grant_tool` ×2 pending |
| `weekly-review` | 7 simulated days, then a company run | Meta campaign (ROAS < 1.5) paused, Google kept; lesson for Marketing proposed |

### Offline vs live
- **Offline** (`--offline` or `LLM_MODE=scripted`): `scenario.script(ctx)` loads per-agent queues into the `ScriptedChatModel`. A step is `{"tool_calls": [...]}`, `{"text": ...}`, or a callable that reads the conversation, for example to use an id returned by a previous tool.
- **Live**: the same `scenario.run(ctx)`, but Claude makes every decision. Outcomes vary, and the policy gate keeps them inside the same guardrails.

## Writing a new scenario
1. Write `setup() -> ctx` (seed data, ids), `run(ctx)` (call a workflow), and `script(ctx)` (offline steps per agent).
2. Register it in `SCENARIOS` in `src/aecom/scenarios.py`.
3. Add a test in `tests/test_scenarios.py` that checks the path, the escalations and the database side effects.

> **Gotcha:** in scripted mode, two tasks for the *same* agent in the same wave would share one script queue. Chain them with `depends_on`.
