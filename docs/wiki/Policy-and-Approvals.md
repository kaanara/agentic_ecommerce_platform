# Policy and Approvals

Every tool call is checked by `src/aecom/policy/gate.py`, using the escalation matrix in `src/aecom/policy/policy.yaml`.

> Limits on `T2` actions can be raised per action, with evidence, by a human, and are lowered automatically: see [Autonomy](Autonomy).

## Tiers
| Tier | Meaning | Result |
|---|---|---|
| T0 | Read-only | allow |
| T1 | Low risk, reversible | allow + audit |
| T2 | Bounded risk | allow inside limits, otherwise **escalate** |
| T3 | High stakes / public / irreversible | always **escalate** |
| T4 | Forbidden for agents | **deny** (also every tool matching `defaults.forbidden_patterns`) |

## Order of checks
1. **Kill switch**: if it's on, every side-effecting call is denied. Reads still work.
2. **Safe mode**: if it's on, every side-effecting call is denied except `defaults.continue_in_safe_mode` (deliveries and customer replies).
3. **Agents paused**: if it's on, every side-effecting call is denied except `defaults.continue_while_paused` (delivery).
4. **Forbidden patterns**: sign-up, login and form tools are denied as T4, read-only or not (see below).
5. **Allowlist**: the agent only receives tools it was granted.
6. **Rule**: tier, agent restriction (`agents:`), then the T2 checks: `limits`, `price_change_pct_max`, `monthly_budget_setting`.
7. Unknown tools (for example from a new MCP server) are treated as T3 when they have side effects. Anything from a **sandboxed** server is at least T3.

## No accounts, no logins
Agents never sign up, log in or submit forms. Creating accounts, accepting terms and entering credentials or payment
methods are human-only; an agent asks for them via `request_provisioning`, which always escalates.

Rules are keyed by exact tool name and MCP tools arrive as `mcp:<server>/<tool>`, so `policy.yaml → defaults.forbidden_patterns`
holds fnmatch patterns (case-insensitive) such as `*login*`, `*sign_in*`, `*create_account*`, `*fill_form*`,
`*browser_type*`, `*browser_click*`, `*payment_method*`. Built-in tool names are matched whole; for MCP tools only the
tool part after `/` is matched, never the server name (R4): `mcp:playwright/browser_fill_form` and `mcp:x/SignIn` are denied,
`mcp:playwright/browser_navigate` follows the normal rules, and a server called `login_service` does not deny its read tools
(`mcp:login_service/list_users` follows the normal rules, `mcp:login_service/login` is still denied).
A match always yields `DENIED by policy (T4): Forbidden for agents: accounts, logins and form submission are human-only.`
No built-in catalog tool may match a pattern (enforced by `tests/test_policy_denylist.py`).

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

The marketing budget is exact per calendar month (R9): committed = marketing spend booked this UTC month (ledger `marketing_expense` lines created since the month start; campaign spend from earlier months does not count, paused or ended campaigns still count) + remaining budget (budget - spent) of the other live campaigns + the requested budget (on a relaunch only the part not yet spent). Escalates when that exceeds `marketing_budget_month_eur`.

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
- **Race-safe de-duplication (R8)**: a partial unique index on `ops_approvals.dedup_key WHERE status='pending'` makes a second pending approval for the same call impossible. If two identical escalations race, the loser's insert is rolled back and it returns the same "already pending" message with the winner's id.
- **Expiry (R8)**: pending approvals expire after `approval_ttl_hours`. The scheduler sweeps every 30 minutes (`expire_approvals`, logs how many); listing also sweeps. `GET /approvals?status=expired` lists them; every approval carries `expires_at`. The dashboard history table shows expired rows with their `expires_at`.
- Approved actions skip re-evaluation. The human *is* the gate for that call.
- MCP calls (`mcp:<server>/<tool>`) are re-invoked through `langchain-mcp-adapters` on approval.

### Interfaces
| Where | How |
|---|---|
| Dashboard | **Approvals** page: reviewer name → Approve & execute / Reject |
| CLI | `aecom approvals`, `aecom approvals approve <id> --by kaan --note "ok"`, `aecom approvals reject <id>` |
| API | `GET /approvals`, `POST /approvals/{id}/approve` `{"by": "...", "note": "...", "args_override": {...}}`, `POST /approvals/{id}/reject` |

## Kill switch, safe mode and agent pause
Three switches, checked first by the gate, for agents, reflexes and external MCP clients alike. Read-only tools always run.

| Switch | Blocks | Still runs | How |
|---|---|---|---|
| **Kill switch** (`kill_switch`) | every side-effecting tool, including delivery; approvals refuse to execute | nothing with side effects | Dashboard Home, `aecom kill-switch on\|off`, `POST /kill-switch?on=true` |
| **Safe mode** (`safe_mode`, plus `safe_mode_since`, `safe_mode_reason`) | every side-effecting tool except `policy.yaml → defaults.continue_in_safe_mode`, so all spending (`launch_campaign`, `register_vendor`, `record_expense`, `refund_order`, `update_price`, ...) | `deliver_order`, `add_license_keys`, `reply_email`, `send_email` (still only to the customer of the case) | Switched **on by the monitors** when a P1 stays open (unacknowledged) for over 2 h; switched **off only by a human**: Dashboard Home button, `aecom safe-mode off --by NAME`, `POST /safe-mode?on=false&by=NAME` |
| **Pause agents** (`agents_paused`) | every side-effecting tool except `policy.yaml → defaults.continue_while_paused` | `deliver_order`, so paying customers still get their keys | Dashboard Home toggle, `POST /agents-pause?on=true` |

The kill switch is checked first and wins: with it on, delivery is denied too, whatever the other switches say. Safe mode is checked before the pause; with both on, only what both allow (delivery) runs. A denied delivery is retried by the event bus and dead-lettered (re-run with `retry`) if the switch stays on, so it is not lost. `GET /health` shows `kill_switch`, `agents_paused` and `safe_mode`. Use the pause to stop agents doing new work; use the kill switch for an emergency.

How they differ: the **kill switch** is a human's emergency stop (nothing with side effects runs, not even delivery). **Pause** is a human's "stop new agent work" (only delivery continues). **Safe mode** is automatic and in between: it exists so that a P1 nobody reacts to makes the company *safer*, not riskier. Customers keep getting their keys and answers to their emails; nothing that costs money runs. The monitors never leave safe mode by themselves, even when the incident resolves, because a human has to look first; leaving it is audited with the human's name (`aecom safe-mode` shows since when and why).

## Withdrawal consent
EU law lets customers withdraw from a purchase for 14 days. For digital content that right ends early only if the customer **expressly agrees** to delivery starting before those 14 days are up and **acknowledges** that this ends their withdrawal right. We also have to keep a record of that consent. The checkout checkbox wording is `aecom.store.mock.WITHDRAWAL_CONSENT_VERSION` (currently `2026-10-v1`, English, pending legal review). Each order stores `withdrawal_consent_at` (the order time) and `withdrawal_consent_version`. An order without consent is still paid and booked in the ledger, but `deliver_order` holds it in status **`awaiting_consent`**: no key is assigned and no email is sent. It goes out only once a human steps in or the customer consents later (`MockStore.record_withdrawal_consent`, which sets the order back to `paid`). The reconciler, the monitors and the chaos invariants only look at `paid`/`failed`, so a held order is never retried in a loop or flagged as undelivered. If you change the wording, also bump the version.

## Budgets
Each agent has a daily LLM budget in € (`POLICY.md`, falling back to `policy.yaml → budgets`). If the budget is spent, the run is refused and audited.

## Audit
`ops_audit_log` is **append-only**: an ORM hook raises if a row is updated or deleted. Every non-T0 decision, every human decision, and every execution result is logged.

## Changing policy
Edit `policy.yaml` and restart the process. The file is cached by `load_policy()`. Agents can't change policy (`change_policy` is T4). Treat policy edits like code: review them in a PR.
