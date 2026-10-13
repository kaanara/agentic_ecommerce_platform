# Data Model

One SQLAlchemy model (`src/aecom/db.py`). Tables are prefixed by domain so the same model runs on SQLite and Postgres.

| Table | Purpose | Key columns |
|---|---|---|
| `company_okrs` | OKRs per pillar | pillar, key_result, metric (a `get_kpis` key), target |
| `company_decisions` | ADRs | category, choice, rationale, alternatives, scorecard, status (accepted/superseded) |
| `company_vendors` | Platforms, suppliers, subscriptions | category, monthly_cost_eur, status, credential_env |
| `company_campaigns` | Marketing campaigns | channel, budget/spent, clicks, conversions, revenue, status |
| `commerce_products` | Digital catalog | sku, kind (license/download/course/subscription), price_eur (VAT incl.), unit_cost_eur |
| `commerce_license_keys` | Key pool | product_id, key, status (available/assigned/revoked), order_id |
| `commerce_customers` | Buyers | email, country (drives VAT), acquisition_channel |
| `commerce_orders` | Orders | amount_eur, vat_eur, campaign_id, status (paid/delivered/refunded/failed) |
| `commerce_refunds` | Refunds | order_id, amount_eur, reason |
| `ledger_entries` | Double-entry book | txn_id, account, debit, credit, ref |
| `registry_tools` | Tools, skills, MCP servers | kind, status, version, config, proposed_by, approved_by |
| `registry_allowlist` | Who may use what | agent_id, tool_name |
| `ops_approvals` | HITL queue | tool_name, args, risk_tier, reason, status, decided_by, result |
| `ops_audit_log` | Append-only audit | actor, action, decision, detail, run_id |
| `ops_events` | Event bus | topic, payload, processed |
| `ops_agent_runs` | Every agent run | agent_id, model, tokens, cost_eur, status, steps |
| `ops_tool_calls` | Every gated call | agent_id, tool_name, decision, ok |
| `ops_outbox_emails` | Sent and received email | direction, to, subject, body, provider (agentmail/mock) |
| `ops_settings` | Runtime settings | kill_switch, monthly budgets |
| `memory_items` | Long-term memory | agent_id (or `shared`), namespace, content |
| `memory_lessons` | Procedural memory proposals | agent_id, lesson, evidence, status |

The LangGraph checkpoints live in a separate file, `data/aecom.checkpoints.db` (on Postgres, the same database).

## Ledger accounts
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
