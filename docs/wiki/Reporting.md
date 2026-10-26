# Reporting

> **Status:** the data exists (ledger, orders, campaigns, agent runs, audit log), but there are **no stored, delivered or exportable reports**. This page records today's state, the gaps and the target design. Work items are tracked in [Roadmap](Roadmap) → section J, and scheduled in the [Development Plan](Development-Plan).

## What exists today
| Report | How it's produced | Where it ends up |
|---|---|---|
| Daily finance report | Scheduler 07:00 → Finance agent | The agent's run output; 300 characters in the process log; "3 insights" written to **shared memory** |
| Weekly review | Scheduler Monday 08:00 → company run | The CEO's executive summary in the run output |
| Business dashboard | Live SQL queries | On screen only (7/30/90-day ranges) |
| `get_pnl` / `get_kpis` tools | Ledger and order queries | Returned to the agent that called them |

## Gaps
- **Nobody receives a report.** No email, no file, no archive. A report exists only as a row in `ops_agent_runs`.
- **No periods.** `get_pnl()` and `get_kpis()` are all-time only, so "last month" or "Q3" can't be answered with the tools.
- **Two sources of truth.** `get_kpis` computes revenue from the `orders` table, while `get_pnl` uses the ledger. They can disagree (refunds, VAT, failed orders).
- **Numbers written by an LLM.** The Finance agent writes the report text itself. Nothing checks that its figures match the database.
- **Reports aren't snapshots.** Re-running later gives different numbers, and no period is ever closed.
- **No tax or accounting output.** There's no VAT breakdown by country and rate (ledger lines carry no country), no journal export for an accountant, and no receipts attached to expenses.
- **Records can be changed.** Ledger rows are ordinary rows (the simulator rewrites `created_at`). German bookkeeping rules (GoBD) require records that can't be altered, kept for 10 years.
- **No export.** The dashboard has no CSV or PDF download.
- **Daily insights pollute shared memory.** Every agent recalls them, they pile up daily, and they're never reviewed (see I15).

## Target design

### Principles
1. **Code computes numbers, the LLM writes commentary.** Every figure comes from one deterministic metrics module. The agent receives the figures and writes the narrative. A check rejects a report whose text quotes a number not in its data.
2. **One source of truth for money: the ledger.** KPIs that involve money are derived from ledger lines, not recomputed from orders.
3. **Reports are immutable snapshots.** Each report stores its period, its data (JSON), its text and a hash. Corrections are new versions, never edits.
4. **Periods get closed.** After month-end close, ledger lines in that month can't change; corrections are booked in the next open period.
5. **Every report has an owner and an audience.**

### Report catalog
| Report | Audience | Cadence | Contents | Delivery |
|---|---|---|---|---|
| **Daily flash** | Owner | Daily 07:00 | Yesterday's orders, net revenue, refunds, ad spend, AI cost, open incidents, pending approvals | Email (merged with the P3 alert digest), dashboard |
| **Weekly business review** | Owner, CEO agent | Monday | KPIs vs OKRs, product and channel performance, decisions made, lessons, next steps | Email + archive |
| **Month-end close** | Owner, accountant | Monthly, by the 5th | P&L, cash, VAT payable, budget vs actual, payout reconciliation, open items | PDF + CSV, archive, period locked |
| **VAT report** | Accountant / tax | Quarterly (OSS) | Sales and refunds by country and VAT rate; with a merchant of record, the MoR statements and payouts instead | CSV |
| **Accountant export** | Accountant | Monthly | General journal in the accountant's format (e.g. DATEV CSV in Germany), with links to receipts | CSV |
| **Product and stock** | Owner, Store Ops, Supply | Weekly | Sales and refund rate per SKU, key stock, burn rate, days of stock left | Dashboard + weekly review |
| **Marketing** | Owner, Marketing | Weekly | Spend, CAC, ROAS and conversion by channel and campaign; attribution | Dashboard + weekly review |
| **Support** | Owner, Support | Weekly | Tickets, response time, refund reasons, top issues | Dashboard + weekly review |
| **AI and agents** | Owner | Weekly | Cost per agent and per order, runs, errors, escalations, approve-unchanged rate, Judge scores | Dashboard + weekly review |
| **Governance and compliance** | Owner, auditor | Monthly | Approvals and who decided, policy denies, kill-switch use, incidents, GDPR requests, consent changes, key rotations | PDF, archive |
| **Decision review** | Owner, CEO agent | Quarterly | ADRs of the period and whether they delivered the expected result | Archive |

### Building blocks
| Block | Purpose |
|---|---|
| `aecom/metrics.py` | The only place KPIs are calculated; every function takes `start`, `end` |
| Period parameters on `get_pnl` / `get_kpis` | So agents can answer "last month" and "Q3" |
| `ops_reports` table | `id, kind, period_start, period_end, data_json, text, version, sha256, created_at, created_by` |
| `submit_report` terminal tool | Finance and CEO agents submit commentary for a report; numbers are attached by code |
| Number check | Every number in the text must appear in `data_json` (allowing for rounding) |
| Period close | `ops_periods` with `open → closed`; posting into a closed period raises |
| VAT detail | Country and rate stored on every VAT ledger line |
| Renderers | Markdown → email, PDF, CSV |
| Reports page | Dashboard archive with filters and downloads |
| `aecom report <kind> --period 2026-10` | CLI to (re)generate a report |
| `GET /reports`, `GET /reports/{id}` | API access (authenticated, see I1) |

### Retention
Accounting records and reports: 10 years (German GoBD / HGB). Operational reports: per the retention policy (H10). Back up the report archive with `data/` (C3).
