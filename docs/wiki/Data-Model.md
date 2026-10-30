# Data Model

One SQLAlchemy model (`src/aecom/db.py`). Tables are prefixed by domain so the same model runs on SQLite and Postgres.

| Table | Purpose | Key columns |
|---|---|---|
| `company_okrs` | OKRs per pillar | pillar, key_result, metric (a `get_kpis` key), target |
| `company_decisions` | ADRs | category, choice, rationale, alternatives, scorecard, status (accepted/superseded) |
| `company_vendors` | Platforms, suppliers, subscriptions | category, monthly_cost_eur, status, credential_env, checklist, verified_at, last_check_error, last_reminded_at |
| `company_campaigns` | Marketing campaigns | channel, budget/spent, clicks, conversions, revenue, status |
| `commerce_products` | Digital catalog | sku, kind (license/download/course/subscription), price_eur (VAT incl.), unit_cost_eur, external_id (store-platform variant id) |
| `commerce_license_keys` | Key pool | product_id, key, status (available/assigned/revoked), order_id |
| `commerce_customers` | Buyers | email, country (drives VAT), acquisition_channel, marketing_consent, marketing_consent_at, marketing_consent_source, unsubscribed_at |
| `commerce_orders` | Orders | amount_eur, vat_eur, campaign_id, status (paid/delivered/partially_refunded/refunded/failed), email_sent, email_sent_at, external_id (unique when set), withdrawal_consent_at, withdrawal_consent_version, download_token, download_expires_at |
| `commerce_refunds` | Refunds | order_id, amount_eur, reason |
| `ledger_entries` | Double-entry book | txn_id, account, debit, credit, ref, country, vat_rate |
| `registry_tools` | Tools, skills, MCP servers | kind, status, version, config, proposed_by, approved_by |
| `registry_allowlist` | Who may use what | agent_id, tool_name |
| `ops_approvals` | HITL queue | tool_name, args, risk_tier, reason, status (pending/approved/rejected/executed/failed/expired), decided_by, result, dedup_key, expires_at |
| `ops_audit_log` | Append-only audit | actor, action, decision, detail, run_id |
| `ops_events` | Event bus | topic, payload, processed, attempts, next_attempt_at, done_handlers, last_error, dead |
| `ops_agent_runs` | Every agent run | agent_id, model, tokens, cost_eur, cost_booked, web_searches, web_fetches, status, steps |
| `ops_tool_calls` | Every gated call | agent_id, tool_name, decision, ok, args_summary, result_summary |
| `ops_outbox_emails` | Sent and received email | direction, to, subject, body, provider (agentmail/mock) |
| `ops_settings` | Runtime settings | kill_switch, monthly budgets, `agent_enabled:<id>` |
| `ops_webhook_receipts` | Webhook idempotency | provider (lemonsqueezy/agentmail), event_key, topic, event_id |
| `ops_incidents` | Monitor incidents | monitor, severity (P1-P3), subject, status (open/acknowledged/resolved), first_seen, last_seen, alert_count, acknowledged_by |
| `ops_reports` | Stored reports (daily flash, weekly review) | kind, period_start, period_end, version, data, text, created_by |
| `ops_errors` | Captured exceptions | component, run_id, event_id, exc_type, message, traceback, context, sentry_event_id |
| `commerce_suppressions` | Email suppression list | email (PK, lower-case), reason (unsubscribe/bounce/complaint/manual) |
| `memory_items` | Long-term memory | agent_id (or `shared`), namespace, content, source_agent, source_run_id, tainted, status (active/pending_review/rejected), reviewed_by |
| `memory_lessons` | Procedural memory proposals | agent_id, lesson, evidence, status |

The LangGraph checkpoints live in a separate file, `data/aecom.checkpoints.db` (on Postgres, the same database).

## Chat, channels, jobs and UI-edited configuration (migration 0010)
| Table | Purpose |
|---|---|
| `ops_conversations` | One chat: agent, channel (`dashboard`, `telegram`, `whatsapp`, `cli`), who started it (`owner` or `agent`), a summary of folded older turns, unread count, total cost, timestamps |
| `ops_messages` | Messages of a conversation (cascade delete): role, masked content, tool steps, status, cost, `agent_run_id` (links to Run detail), escalation approval ids, attachment metadata |
| `ops_channel_links` | Paired messaging chats (unique per channel and chat id), `active`, and `max_approve_tier`, the highest tier approvable from that chat |
| `ops_jobs` | UI-requested background jobs run by the scheduler: kind `backup`, `verify`, `restore`, `doctor` or `update` (recorded by `update.sh`), status, result JSON |
| `ops_policy_versions` | Versions of `policy.yaml` saved from the Settings page; the single active row wins over the file in git |
| `ops_periods` | Accounting months: status (open or closed), who closed it and when, a fingerprint of its ledger lines (tamper check) and the month-end report id (migration 0011) |
| `ops_decision_reviews` | The quarterly look back at a decision: outcome, note and reviewer (one per decision and quarter) |
| `ops_persona_overrides` | Persona and `company/` file edits made in the UI (scope, file name, content, hash of the git file at edit time); the loader prefers them |

UI settings are rows in `ops_settings` with the key `config:<name>`, plus `config_version`, which every process checks to reload. Secrets are never stored in the database: they go to `.env` only.

## Ledger accounts
The ledger is append-only: editing or deleting a `ledger_entries` row raises an error (only the market simulator may back-date demo data). Corrections are new balancing entries, and nothing deletes ledger rows, so the 10-year retention holds.

| Account | Normal side | Written by |
|---|---|---|
| `cash` | debit | sales, refunds, expenses |
| `revenue` | credit | `record_sale` (net of VAT) |
| `vat_payable` | credit | `record_sale` (customer-country VAT, OSS) |
| `cogs` | debit | `record_sale` (unit cost) |
| `refunds` | debit | `record_refund` |
| `marketing_expense` | debit | simulator / ad platforms |
| `ai_expense` | debit | every agent run |
| `vendor_expense` | debit | `record_expense` |

## Partial unique indexes
| Index | Table / columns | Applies where | Guarantees |
|---|---|---|---|
| `uq_approvals_pending_dedup` | `ops_approvals(dedup_key)` | `status = 'pending' AND dedup_key IS NOT NULL` | Two identical escalations racing each other can't both be pending; the loser reuses the winner's row |
| `uq_incidents_active` | `ops_incidents(monitor, subject)` | `status IN ('open', 'acknowledged')` | At most one active incident per monitor and subject |
| `uq_commerce_orders_external_id` | `commerce_orders(external_id)` | `external_id IS NOT NULL` | One local order per store-platform order, even if two webhook deliveries race |

Inserting a duplicate raises `sqlalchemy.exc.IntegrityError`.

`ledger.post()` rejects any unbalanced transaction. `ledger.pnl()` derives net revenue, gross margin, net profit, cash and VAT payable.

## Useful queries
```sql
-- AI cost per agent today
SELECT agent_id, ROUND(SUM(cost_eur), 4) FROM ops_agent_runs WHERE date(started_at) = date('now') GROUP BY agent_id;
-- What did the gate escalate or deny?
SELECT ts, actor, action, decision, detail FROM ops_audit_log WHERE decision IN ('escalate','deny') ORDER BY id DESC;
-- ROAS per campaign
SELECT name, channel, spent_eur, revenue_eur, revenue_eur / NULLIF(spent_eur,0) AS roas FROM company_campaigns;
```
