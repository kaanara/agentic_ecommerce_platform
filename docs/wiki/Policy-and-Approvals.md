# Policy and Approvals

Every tool call is checked by `src/aecom/policy/gate.py`, using the escalation matrix in `src/aecom/policy/policy.yaml`.

## Tiers
| Tier | Meaning | Result |
|---|---|---|
| T0 | Read-only | allow |
| T1 | Low risk, reversible | allow + audit |
| T2 | Bounded risk | allow inside limits, otherwise **escalate** |
| T3 | High stakes / public / irreversible | always **escalate** |
| T4 | Forbidden for agents | **deny** |

## Order of checks
1. **Kill switch**: if it's on, every side-effecting call is denied. Reads still work.
2. **Allowlist**: the agent only receives tools it was granted.
3. **Rule**: tier, agent restriction (`agents:`), then the T2 checks: `limits`, `price_change_pct_max`, `monthly_budget_setting`.
4. Unknown tools (for example from a new MCP server) are treated as T3 when they have side effects. Anything from a **sandboxed** server is at least T3.

## Current matrix (highlights)
| Action | Tier | Auto-allowed when | Otherwise |
|---|---|---|---|
| `refund_order` (support, finance) | T2 | ≤ €50 | human |
| `send_email` (support, store_ops, marketing) | T2 | recipient is the customer of the current case | human |
| `update_price` (store_ops, finance) | T2 | change ≤ 20% | human |
| `launch_campaign` (marketing) | T2 | ≤ €150 **and** within monthly marketing budget | human |
| `register_vendor` (supply, foreman) | T2 | ≤ €50/month **and** within vendor budget | human |
| `record_expense` (finance) | T2 | ≤ €200 | human |
| `publish_social_post`, `send_newsletter` | T3 | never | human |
| `request_provisioning` (ceo) | T3 | never | human |
| `approve_tool`, `grant_tool` (foreman) | T3 | never | human |
| `delete_customer_data`, `change_policy` | T4 | never | denied |

Monthly budgets live in `ops_settings` (`marketing_budget_month_eur` = 1500, `vendor_budget_month_eur` = 400 by default).

## What the agent sees
- `DENIED by policy (T2): refund_order is reserved for support, finance.`
- `ESCALATED to human approval (T2, id=apr_…): amount_eur=79 exceeds limit 50 …`. The agent continues its task and reports the pending id.
- `ERROR: …`: the tool raised. The model gets the message and can recover.

## The approval flow
```
agent tool call ──gate: escalate──▶ ops_approvals (pending: tool, args, tier, reason, context, run_id)
                                         │
             Dashboard / API / CLI ◀─────┘
       approve(by, note, args_override?) ──▶ execute exact call as the original agent ──▶ executed | failed
       reject(by, note)                  ──▶ rejected
                       both ──▶ audit log + event `approval.resolved`
```
- **Edit before approving**: change JSON args in the dashboard, or pass `args_override` via the API. Only changed keys are overridden.
- Approved actions skip re-evaluation. The human *is* the gate for that call.
- MCP calls (`mcp:<server>/<tool>`) are re-invoked through `langchain-mcp-adapters` on approval.

### Interfaces
| Where | How |
|---|---|
| Dashboard | **Approvals** page: reviewer name → Approve & execute / Reject |
| CLI | `aecom approvals`, `aecom approvals approve <id> --by kaan --note "ok"`, `aecom approvals reject <id>` |
| API | `GET /approvals`, `POST /approvals/{id}/approve` `{"by": "...", "note": "...", "args_override": {...}}`, `POST /approvals/{id}/reject` |

## Kill switch
Dashboard Home, `aecom kill-switch on|off`, or `POST /kill-switch?on=true`. It applies to agents, reflexes and external MCP clients alike.

## Budgets
Each agent has a daily LLM budget in € (`POLICY.md`, falling back to `policy.yaml → budgets`). If the budget is spent, the run is refused and audited.

## Audit
`ops_audit_log` is **append-only**: an ORM hook raises if a row is updated or deleted. Every non-T0 decision, every human decision, and every execution result is logged.

## Changing policy
Edit `policy.yaml` and restart the process. The file is cached by `load_policy()`. Agents can't change policy (`change_policy` is T4). Treat policy edits like code: review them in a PR.
