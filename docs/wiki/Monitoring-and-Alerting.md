# Monitoring and Alerting

> **Status:** deterministic monitors run every 5 minutes in `aecom scheduler`, open incidents in `ops_incidents` (deduplicated, auto-resolved), and alert a human: P1 by phone push (ntfy) and email, P2 by email. An external dead-man's switch (healthchecks.io) is pinged after every monitor run. `/health` reports real state and returns HTTP 503 when unhealthy. Still planned: P3 daily digest, a Health dashboard page, the Sentinel agent and the remaining monitors (see *Target design*). Work items are tracked in [Roadmap](Roadmap) → *Production readiness backlog*.

## What exists today
| Signal | Where you see it | Alerts? |
|---|---|---|
| Monitors and incidents (below) | `aecom incidents`, `ops_incidents` | ✅ P1 push + email, P2 email |
| Agent runs (status, steps, cost, task, output) | Dashboard → Agents → Recent runs | ✅ stuck runs (P2) |
| Policy decisions (allow / escalate / deny), errors | Dashboard → Agents → Audit trail (`ops_audit_log`), `aecom errors` (`ops_errors`) | ✅ any `ops_errors` row in the last hour (P2) |
| Event handler failures / dead letters | Audit trail, `aecom events dead` | ✅ order/inventory dead letter P1, others P2 |
| Pending approvals | Dashboard → Home / Approvals | ✅ older than 24 h (P2) |
| Business KPIs, ROAS, OKRs | Dashboard → Business | ❌ |
| LLM cost per agent, daily budgets | Dashboard → Agents; budget check before each run | ❌ (silently defers the task) |
| Kill switch state | Dashboard → Home, `/health` | ✅ on for more than 1 h (P2) |
| Full model and tool traces | LangSmith, if `LANGSMITH_TRACING=true` | ❌ |
| Process alive | `systemctl status` / `journalctl` on the Pi; external heartbeat | ✅ via healthchecks.io when pings stop |
| `/health` endpoint | API (bound to `127.0.0.1`), unauthenticated; `?detail=true` adds `db`, `open_incidents`, `event_bus` | 503 when the DB is unreachable or a P1 incident is active |

## Monitors (`src/aecom/monitors.py`)
Plain SQL checks, no LLM, so they work when Claude is down or a budget is exhausted. Each monitor reports its current problems as (subject, detail); a problem opens or refreshes the incident for (monitor, subject) and subjects that are no longer reported are **auto-resolved**. Monitors are isolated: one that raises becomes a P2 `monitor_error` incident (subject = monitor name) and the rest still run. Thresholds are module constants.

| Sev | Monitor | Fires when | Subject |
|---|---|---|---|
| P1 | `undelivered_order` | order `paid` or `failed` for more than 15 min | order id |
| P1 | `unemailed_order` | order delivered but `email_sent` false for more than 15 min | order id |
| P1 | `ledger_imbalance` | debits ≠ credits (overall or per transaction) | `ledger` |
| P1 | `duplicate_key` | a licence key on two orders, or more keys than the quantity (detail names key ids and order ids, never the key value) | `license_keys` |
| P1 | `order_dead_letter` | dead-lettered event with topic `order.*` or `inventory.*` | event id |
| P1 | `restock_failed` | `inventory.restock_failed` event in the last 24 h | sku |
| P2 | `other_dead_letter` | any other dead-lettered event | event id |
| P2 | `event_backlog` | more than 100 waiting/queued events, or the oldest due event is older than 10 min | `event_bus` |
| P2 | `stuck_run` | agent run `running` for more than 30 min | run id |
| P2 | `errors` | rows in `ops_errors` in the last hour (one incident with the count) | `ops_errors` |
| P2 | `approval_ageing` | pending approval older than 24 h | approval id |
| P2 | `kill_switch_long` | kill switch on for more than 1 h (first seen on is stored in setting `kill_switch_since`) | `kill_switch` |
| P2 | `monitor_error` | a monitor itself raised | monitor name |

## Alert routing (`src/aecom/alerts.py`)
| Sev | Channel | Timing |
|---|---|---|
| P1 | ntfy phone push (title, priority high) + email to `ALERT_EMAIL` | when the incident opens, repeated every 30 min until acknowledged |
| P2 | email to `ALERT_EMAIL` | once, when the incident opens |
| P3 | none yet (daily digest planned) | - |

- Acknowledge to stop repeats: `uv run aecom incidents ack <id> --by <you>`; resolve by hand with `aecom incidents resolve`, or let the monitor auto-resolve it.
- Email goes through AgentMail and stays in the local outbox unless `AGENTMAIL_LIVE=true`. The push is a plain HTTP POST, so it does not depend on AgentMail or Claude.
- Every channel is a no-op without its config. Network errors are logged and recorded in `ops_errors` (without the URL) but never break the monitor loop. Alert text is secret-masked.

### Configuration (`.env`)
| Variable | Purpose |
|---|---|
| `NTFY_URL` | Full ntfy topic URL, e.g. `https://ntfy.sh/<unguessable-topic>`. Install the ntfy app on your phone and subscribe to the same topic. |
| `NTFY_TOKEN` | Optional access token for a protected topic (sent as `Authorization: Bearer`). |
| `ALERT_EMAIL` | Recipient of P1 and P2 alert emails. |
| `HEALTHCHECKS_PING_URL` | Ping URL of an external dead-man's switch, e.g. a healthchecks.io check (`https://hc-ping.com/<uuid>`). Set its period to 5 min and grace to 10 min, and route its own alerts to your phone. |

The heartbeat is pinged **after** each monitor run, so the external service only hears from us when the monitor loop really ran. If the Pi, the scheduler or the network is down, the pings stop and *that* service alerts you.

## Known blind spots
These failures are **neither detected nor alerted** today:
- ~~**A paid order never gets delivered.**~~ Fixed (A1): events are retried with backoff and dead-lettered after 5 attempts (`aecom events dead`); undelivered orders and dead letters now alert (B).
- ~~**An agent run crashes.**~~ Fixed (A3): crashed runs are marked `error` with the message.
- ~~**The Pi, the scheduler or the internet connection is down.**~~ Covered once `HEALTHCHECKS_PING_URL` is set (external heartbeat).
- **The kill switch stops deliveries.** Reflexes go through the gate, so paying customers stop receiving keys (now at least alerted: `kill_switch_long` after 1 h, `undelivered_order` after 15 min).
- **Payment received but webhook lost**, chargebacks, refund abuse below the per-refund limit (no reconciler, no pattern checks).
- ~~**Ledger imbalance, double-assigned licence keys.**~~ P1 monitors (B).
- **Disk full, SD card wear, stale backups.**
- **The provisioning chain stalls.** `approval.resolved` has no subscriber, and vendors stay `provisioning` after the credential is added.

## Target design

### 1. Monitors: deterministic checks, no LLM
*Implemented for the monitors listed above; the rest of this table is still planned (LLM spend, refund anomaly, host, external service, business drift, policy-deny spike, error rate).* A scheduler job runs every check every 5 minutes. Checks are SQL or system queries, so they work when Claude is down or an agent budget is exhausted.

| Sev | Monitor | Fires when |
|---|---|---|
| P1 | Undelivered paid order | `orders.status = 'paid'` older than 15 min |
| P1 | Heartbeat missed (**external**) | No ping to the external monitor for 10 min (Pi, scheduler or network down) |
| P1 | Ledger imbalance | Sum of debits ≠ sum of credits |
| P1 | Duplicate licence key | One key assigned to more than one order |
| P1 | Order-path handler error | Any `event:order.*` / `event:inventory.*` error in the audit log |
| P2 | Event backlog | Unprocessed events > 100, or the oldest one is > 10 min old |
| P2 | Dead-letter events | Any event moved to dead letter after N retries |
| P2 | Stuck or failing agents | Run `running` > 30 min, or error rate > 20% in the last hour |
| P2 | LLM spend | An agent reaches 80% of its daily budget, or the Anthropic API keeps failing |
| P2 | Refund anomaly | Refund rate > 10% per day, or more than 2 refunds per customer in 30 days |
| P2 | Approval ageing | A pending approval is older than 24 h |
| P2 | Stock | Keys below threshold **and** the restock failed |
| P2 | Host | Disk > 85%, last backup older than 26 h |
| P2 | Kill switch | On for more than 1 h |
| P2 | External service | AgentMail, store platform or MCP server failing |
| P3 | Business drift | ROAS < 1.5 after €100 spend; revenue or conversion down > 50% vs. the 7-day average |
| P3 | Policy-deny spike | Denies per agent well above normal (misbehaviour or prompt injection) |

### 2. Alert routing
*Implemented for P1 and P2 (see above); the P3 daily digest and Telegram/Pushover are still planned.*

| Sev | Meaning | Channel | Timing |
|---|---|---|---|
| P1 | Money or customers at risk | Phone push (ntfy, Telegram or Pushover) + email | Immediately, repeat every 30 min until acknowledged |
| P2 | Degraded, no direct loss yet | Email | Within 1 h |
| P3 | Worth knowing | Daily digest email | 07:00 |

- **P1 must not depend on the Pi alone.** The scheduler pings an external dead-man's switch (for example healthchecks.io). If the pings stop, *that* service alerts you.
- Push notifications are a plain HTTP POST, so they don't depend on AgentMail or Claude.
- One alert per incident (deduplicated by monitor + subject), auto-resolve when the check passes again, and an acknowledge button on the dashboard.

### 3. Incidents
*`ops_incidents`, dedup, auto-resolve, acknowledge (CLI) and the real `/health` exist. Still planned: the dashboard Health page with an acknowledge button, and the last scheduler tick in `/health`.*

- New table `ops_incidents` (monitor, severity, subject, first and last seen, status `open | acknowledged | resolved`, notes).
- Dashboard page **Health**: current monitor states, open incidents, recent alerts, plus the chaos-test invariants once they exist.
- `/health` reports real state: DB reachable, last scheduler tick, event backlog, open P1 incidents. It returns HTTP 503 when unhealthy.

### 4. Sentinel agent: diagnosis, not detection
A read-only Ops agent (Haiku or Sonnet) runs daily and whenever a P1 or P2 incident opens. It reads incidents, audit errors and dead-letter events, writes a plain-language incident report, and proposes fixes as approvals or lessons. It is **not** the safety net: the monitors and the external heartbeat are, because they keep working when the app or the Claude API is down.

### 5. Proof with chaos tests
Every monitor gets a fault-injection test (`aecom chaos …`, see the Roadmap) that triggers the failure and asserts the alert fires and then resolves.
