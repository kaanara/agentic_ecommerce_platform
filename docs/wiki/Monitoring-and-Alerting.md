# Monitoring and Alerting

> **Status:** monitoring is *pull only* (you open the dashboard). There is **no alerting**: nothing notifies a human when something breaks. This page records what exists today, the gaps, and the target design. The work items are tracked in [Roadmap](Roadmap) → *Production readiness backlog*.

## What exists today
| Signal | Where you see it | Alerts? |
|---|---|---|
| Agent runs (status, steps, cost, task, output) | Dashboard → Agents → Recent runs | ❌ |
| Policy decisions (allow / escalate / deny), errors | Dashboard → Agents → Audit trail (`ops_audit_log`) | ❌ |
| Event handler failures | Audit trail (`event:<topic>` · `error`) and the process log | ❌ |
| Pending approvals | Dashboard → Home / Approvals | ❌ (no notification, no ageing) |
| Business KPIs, ROAS, OKRs | Dashboard → Business | ❌ |
| LLM cost per agent, daily budgets | Dashboard → Agents; budget check before each run | ❌ (silently defers the task) |
| Kill switch state | Dashboard → Home, `/health` | ❌ |
| Full model and tool traces | LangSmith, if `LANGSMITH_TRACING=true` | ❌ |
| Process alive | `systemctl status` / `journalctl` on the Pi | ❌ |
| `/health` endpoint | API (bound to `127.0.0.1`) | Always returns `ok: true`; checks nothing |

## Known blind spots
These failures are **neither detected nor alerted** today:
- ~~**A paid order never gets delivered.**~~ Fixed (A1): events are retried with backoff and dead-lettered after 5 attempts (`aecom events dead`). Still missing: an **alert** when something lands in the dead letter.
- ~~**An agent run crashes.**~~ Fixed (A3): crashed runs are marked `error` with the message.
- **The Pi, the scheduler or the internet connection is down.** Nothing outside the Pi notices.
- **The kill switch stops deliveries.** Reflexes go through the gate, so paying customers stop receiving keys.
- **Payment received but webhook lost**, chargebacks, refund abuse below the per-refund limit (no reconciler, no pattern checks).
- **Ledger imbalance, double-assigned licence keys** (no invariant checks).
- **Disk full, SD card wear, stale backups.**
- **The provisioning chain stalls.** `approval.resolved` has no subscriber, and vendors stay `provisioning` after the credential is added.

## Target design

### 1. Monitors: deterministic checks, no LLM
A scheduler job runs every check every 5 minutes. Checks are SQL or system queries, so they work when Claude is down or an agent budget is exhausted.

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
| Sev | Meaning | Channel | Timing |
|---|---|---|---|
| P1 | Money or customers at risk | Phone push (ntfy, Telegram or Pushover) + email | Immediately, repeat every 30 min until acknowledged |
| P2 | Degraded, no direct loss yet | Email | Within 1 h |
| P3 | Worth knowing | Daily digest email | 07:00 |

- **P1 must not depend on the Pi alone.** The scheduler pings an external dead-man's switch (for example healthchecks.io). If the pings stop, *that* service alerts you.
- Push notifications are a plain HTTP POST, so they don't depend on AgentMail or Claude.
- One alert per incident (deduplicated by monitor + subject), auto-resolve when the check passes again, and an acknowledge button on the dashboard.

### 3. Incidents
- New table `ops_incidents` (monitor, severity, subject, first and last seen, status `open | acknowledged | resolved`, notes).
- Dashboard page **Health**: current monitor states, open incidents, recent alerts, plus the chaos-test invariants once they exist.
- `/health` reports real state: DB reachable, last scheduler tick, event backlog, open P1 incidents. It returns HTTP 503 when unhealthy.

### 4. Sentinel agent: diagnosis, not detection
A read-only Ops agent (Haiku or Sonnet) runs daily and whenever a P1 or P2 incident opens. It reads incidents, audit errors and dead-letter events, writes a plain-language incident report, and proposes fixes as approvals or lessons. It is **not** the safety net: the monitors and the external heartbeat are, because they keep working when the app or the Claude API is down.

### 5. Proof with chaos tests
Every monitor gets a fault-injection test (`aecom chaos …`, see the Roadmap) that triggers the failure and asserts the alert fires and then resolves.
