# Reporting

> **Status:** stored report snapshots exist (J3) and their commentary is number-checked (J4): `aecom/reports.py` + the `submit_report` tool, persisted in `ops_reports`. The daily flash (J5), the weekly business review (J6) and the monthly governance report (J12) are generated on schedule and emailed; the dashboard **Reports** page and `GET /reports` serve the archive with Markdown and CSV exports (J13). Still missing: PDF, a CLI, month-end close and accounting exports, and periods aren't closed. This page records today's state, the remaining gaps and the target design. Work items are tracked in [Roadmap](Roadmap) → section J, and scheduled in the [Development Plan](Development-Plan).

## What exists today
| Report | How it's produced | Where it ends up |
|---|---|---|
| Daily flash | Scheduler 07:00 → code + Finance commentary (`aecom/flash.py`) | `ops_reports` (`daily_flash`), email |
| Weekly business review | Scheduler Monday 08:00 → `reports.send_report("weekly_review", "last_7_days")` | `ops_reports` (`weekly_review`), email |
| Weekly company run | Scheduler Monday 08:00 → company run | The CEO's executive summary in the run output |
| Governance report | Scheduler 1st of the month 07:30 → `reports.send_report("governance", "last_month")` | `ops_reports` (`governance`), email |
| Reports page / `GET /reports` | Stored snapshots | Dashboard (Markdown and CSV download), API |
| Business dashboard | Live SQL queries | On screen only (7/30/90-day ranges) |
| `get_pnl` / `get_kpis` tools | Ledger and order queries | Returned to the agent that called them |

## How reports are built
1. The Finance (or CEO) agent calls `get_pnl(period=...)` and `get_kpis(period=...)` and writes commentary.
2. It finishes with the terminal tool `submit_report(kind, period, text)` (policy T1, finance and ceo only).
3. `aecom.reports.create_report` recomputes the numbers in code with `build_data(kind, period)`: the period's name,
   start, end and length in days, plus `metrics.pnl` and `metrics.kpis` for that window. The agent never supplies figures.
4. `number_check(text, data)` extracts every number from the text (`1,234.56`, `1.234,56`, `12.5%`, `€49`, `-3`) and
   rejects any that doesn't equal a value in the data after rounding to the precision written (`40.8` matches 40.83,
   `71%` matches 71.4). Years (1900-2100), ISO dates, list markers (`1.`, `2)`) and small counts followed by a word
   (`3 insights`) are ignored. Numbers glued to letters are still checked: scale suffixes are applied (`€1.2k` is
   checked as 1,200, `2 million` as 2,000,000) and other suffixes (`37x`, `250EUR`, `12pct`) keep the number. A failure raises `ValueError` and goes back to the agent as an `ERROR` so it can rewrite;
   nothing is stored.
5. The report is inserted as a new row in `ops_reports` with `data`, `text`, `created_by` and
   `sha256 = sha256(canonical JSON(data) + text)`. `version` is the previous highest version for the same kind and
   period plus one; existing rows are never updated. Read them with `get_report(id)`, `list_reports(kind)` and `latest(kind)`.

## Daily flash
`aecom/flash.py` (J5), run by the scheduler job `finance_daily` at 07:00 (every 10 minutes with `--demo`).
1. `build_flash()` computes everything in code: yesterday's orders, net revenue, refunds, marketing spend, AI cost and
   AI cost per order (`aecom.metrics`, period `yesterday`); active incidents by severity with their titles; pending
   approvals and the age of the oldest; event bus status; the kill switch, `agents_paused` and `safe_mode` flags.
2. Finance gets that data as JSON and replies with 3-5 sentences of commentary. It's stored with
   `create_report("daily_flash", "yesterday", text, created_by="finance")`, so the number check applies.
3. **The flash always goes out.** If the kill switch is on, agents are paused, Finance is disabled, its run fails, or
   the number check rejects its text, a numbers-only report is stored instead (`created_by="system"`, a deterministic
   summary of the figures) and the email says why. `fallback_reason` in the result carries the same reason.
4. The email goes via AgentMail to `REPORT_EMAIL` (or `ALERT_EMAIL` if unset), subject `Daily flash <date>`: the
   commentary, a plain-text table of all the numbers, and an **Open issues** list of active P2/P3 incidents. That list
   is the P3 alert digest: P3 incidents have no other channel. The body is secret-masked. With neither address set,
   the report is still stored and nothing is sent. Like every AgentMail message it stays in the local outbox unless
   `AGENTMAIL_LIVE=true`.
5. Nothing is written to shared memory; daily notes live in `ops_reports` as `daily_flash` versions.

## Weekly business review and governance report
`build_data(kind, period)` always holds `period`, `pnl` and `kpis`; two kinds add their own sections, all computed by
code from the database (periods are half-open `[start, end)` in UTC, see `aecom.metrics.period`):

| Kind (period) | Section | Contents |
|---|---|---|
| `weekly_review` (`last_7_days`) | `products` | Per SKU: orders placed, net revenue and refunds booked in the ledger, refunded orders, refund rate (refunded orders / orders placed) |
| | `campaigns` | Per campaign with spend or revenue in the period: ad spend (`marketing_expense` lines whose ref is the campaign), net revenue of its orders, ROAS |
| | `support` | Inbound emails received; threaded replies sent (outbound subjects starting `Re:`) |
| | `ai` | Ledger AI cost and cost per order (from `kpis`), runs and `cost_eur` per agent from `ops_agent_runs` |
| `governance` (`last_month`) | `approvals` | Created in the period; decided in the period with tool, agent, tier, outcome and who decided; counts by outcome and decider |
| | `policy_denies` | Gate denies (`ops_tool_calls.decision = deny`) by agent |
| | `controls` | `kill_switch`, `agents_paused` and `safe_mode` changes from the audit log, with actor and new state |
| | `incidents` | Opened (`first_seen`) and resolved (`resolved_at`) by severity |
| | `gdpr` | Marketing opt-ins, unsubscribes and suppressions from the audit log; new suppressions by reason |
| | `provisioning` | `provisioning` and `account_need` audit events (activations, checklists, rejections, decisions) by decision, with vendor |

`reports.generate(kind, period)` stores a new version. Without commentary the text is `reports.summary(data)`, a
deterministic numbers-only summary: every figure is formatted from the data, and a line that would still fail the
number check (a product or campaign name containing a stray number) is dropped, so storing never fails. Agents can
still add commentary with `submit_report(kind, period, text)`; it gets the same data and the same check.

`reports.send_report(kind, period)` generates the report and emails it as Markdown to `REPORT_EMAIL` (or `ALERT_EMAIL`),
secret-masked; with neither set it's only stored, and an email failure is recorded in `ops_errors` without losing the
report. Scheduler jobs: `weekly_review_report` (Monday 08:00, next to the `weekly_review` company run) and
`governance_report` (1st of the month 07:30); with `--demo` every 60 and 240 minutes. `scheduler.build_scheduler(demo)`
returns the configured scheduler without starting it.

Not yet in the governance data: who switched the kill switch or the agent pause. Those settings are changed with
`set_setting` and only appear once that writes an audit row (safe mode already does).

## Reports page, API and exports
- **Dashboard → Reports** (`dashboard/app_pages/reports.py`, behind the login): pick a kind, see every version with its
  period, author, time and hash, read the text and the flattened data, and download **Markdown** (text plus a
  key/value table of the data, `reports.to_markdown`) or **CSV** (`key,value` rows, `reports.to_csv`). Nested data is
  flattened to dotted keys with list indexes, e.g. `products.0.refund_rate_pct`.
- **API** (bearer token): `GET /reports?kind=&limit=` (newest first, default 20, max 200) and `GET /reports/{id}`
  (`data`, `text`, `sha256`, `version`, period, author; 404 if unknown).

## Gaps
- **No PDF and no archive outside the database.** Reports are emailed and kept in `ops_reports`; there is no file export beyond the dashboard downloads.
- **No periods.** `get_pnl()` and `get_kpis()` are all-time only, so "last month" or "Q3" can't be answered with the tools.
- **Two sources of truth.** `get_kpis` computes revenue from the `orders` table, while `get_pnl` uses the ledger. They can disagree (refunds, VAT, failed orders).
- **Numbers written by an LLM.** The Finance agent writes the report text itself. Nothing checks that its figures match the database.
- **Reports aren't snapshots.** Re-running later gives different numbers, and no period is ever closed.
- **No tax or accounting output.** There's no VAT breakdown by country and rate (ledger lines carry no country), no journal export for an accountant, and no receipts attached to expenses.
- **Records can be changed.** Ledger rows are ordinary rows (the simulator rewrites `created_at`). German bookkeeping rules (GoBD) require records that can't be altered, kept for 10 years.
- **Partial export.** Stored reports download as Markdown and CSV; there is no PDF, and the Business page has no download.
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
