# Incident Response

> **Status:** not built. Detection, diagnosis and repair are spread across other plans ([Monitoring and Alerting](Monitoring-and-Alerting), the Sentinel agent, the Engineer agent). This page joins them into one process and states who does what. Work items: [Roadmap](Roadmap) → section K.

## The lifecycle
```
detect ──▶ triage ──▶ contain ──▶ diagnose ──▶ fix ──▶ verify ──▶ learn
monitors    severity    safe        Sentinel     depends   monitor     postmortem
(code)      + owner     actions     agent        on type   clears +    → regression
                        (code)                   (below)   chaos test    test + lesson
```

## Who fixes what
| Problem type | Example | Detects | Diagnoses | Fixes | Human needed? |
|---|---|---|---|---|---|
| **Known operational** | Undelivered order, failed email, event in dead letter | Monitor | — | **Automatic** (reconciler, retry, resend) | No; alert only if auto-fix fails |
| **Business judgment** | ROAS < 1, refund spike, vendor outage, stock running out | Monitor | Sentinel | **Agent** with its normal tools: pause campaign, switch to fallback vendor, restock from the real feed | Only if the action escalates under policy |
| **Data inconsistency** | Ledger imbalance, key on two orders, order without ledger lines | Invariant monitor | Sentinel | **Human**, using a correction prepared by Sentinel (a correcting entry, never an edit) | Yes: money and records |
| **Code bug** | Exception in a tool, wrong VAT calculation | Error monitor | Sentinel + Engineer agent | **Engineer agent opens a PR** with a failing test and the fix | Yes: review, merge, deploy |
| **Agent misbehaviour** | Repeated policy denies, prompt injection, bad decisions | Deny-spike monitor, Judge | Sentinel | **Automatic demotion** of autonomy; a lesson or prompt change via PR | Yes: approve the lesson or PR |
| **Infrastructure** | Pi offline, disk full, SD card failing | External heartbeat, host monitor | — | **Human** (systemd restarts processes on its own) | Yes: physical access |
| **External service** | Claude API, AgentMail or store platform down | Error monitor | Sentinel | **Wait and retry**; reflexes keep selling; switch to a fallback if there is one | Only if the outage lasts |
| **Security** | Leaked key, unknown login, suspicious API calls | Monitors, audit | Sentinel | **Contain automatically** (pause agents), then **human** rotates keys | Yes, always |

## Rules
- **Containment comes first and never needs an LLM.** Pause agents, retry, keep delivering. Diagnosis can wait; customers can't.
- **Agents can fix the business, not the system.** They use their existing gated tools. Changing code, policy, ledger history or credentials is always a human decision.
- **The Sentinel diagnoses and proposes; it doesn't act alone.** Its remediation tools go through the policy gate like any other agent's.
- **Every fixed bug leaves a regression test.** The incident becomes a chaos test, so the same bug can't return unnoticed.
- **If you don't respond, the system gets safer, not riskier.** A P1 that's unacknowledged after 2 hours puts the company in **safe mode**: only deliveries, restocking and customer replies pass the gate; all spending stops. Only a human can leave it (see Safe mode below).

## What has to be built
### Evidence: today errors lose their details
- Agent crashes are stored as `"Type: message"` in the audit log, with **no traceback**.
- Tool errors are returned to the model as text, while `ops_tool_calls` records only `ok = False`, **without the error message**.
- Tracebacks from the event bus exist only in the process log (`journalctl`), which rotates.

So neither you nor an agent can find the cause afterwards. Needed: an **`ops_errors`** table (time, component, run id, event id, exception type, message, traceback, input summary), written by the agent loop, the gate, the event bus and the API. Secrets are masked (I16).

### Diagnosis
- **Diagnostic tools for the Sentinel** (read-only): open incidents, recent errors with tracebacks, the run timeline of a `run_id` (D2), health checks, event backlog and dead letter, recent deploys.
- **`aecom doctor`**: one CLI command that checks the DB, invariants, backlog, scheduler, disk, backups, credentials present, and the deployed version; prints a report to paste into an issue.
- **Runbooks**: one per monitor (what it means, what to check, which actions are safe), stored as **skills** so the Sentinel and you follow the same steps.

### Repair
- **Sentinel remediation tools**, all gated: replay a dead-letter event, resend a delivery email, re-run the reconciler, pause agents, open or close an incident. Higher-impact actions escalate to you.
- **Incident → GitHub issue → PR.** Code bugs become a GitHub issue automatically, with the error, traceback, run timeline and a reproduction attempt. The Engineer agent (for example Claude Code in GitHub Actions) picks it up and opens a PR with a failing test and the fix. CI runs; you review and merge; `scripts/update.sh` deploys. The Engineer works in GitHub, never on the Pi, and never deploys.
- **Correcting entries** for data problems: Sentinel prepares the correction as a T3 approval; the ledger stays append-only (J11).
- **Remote access for you** via Tailscale SSH, also from your phone.

### Learning
- **Incident record** in `ops_incidents`: owner, timeline, root cause, fix, linked approvals and PRs.
- **Postmortem** for every P1, written as a draft by the Sentinel and finished by you: what happened, impact (orders, €), why, what changes.
- **Regression test** from each postmortem; **lessons** for agents through the lessons loop.
- Incidents and postmortems appear in the governance report (J12).

### Escalation
- **On-call:** today only you. Name a **backup person** with dashboard access for when you're unavailable.
- **Safe mode** after an unacknowledged P1 (see Rules).

## Using incidents
The incident record (`ops_incidents`) and its lifecycle are built (K2, `aecom.incidents`); the monitors that raise incidents come next.
- **One active incident per (monitor, subject).** `raise_incident(monitor, severity, subject, detail)` opens one, or refreshes the open/acknowledged one (last seen, detail). Severity can go up (P3 → P1) while it is active, never down; an escalation resets the alert state so it alerts again at the next monitor run. Two concurrent raisers for the same pair are safe (a unique index on active incidents; the loser refreshes the winner's row). `resolve_absent` takes a collection of subjects, never a bare string. A resolved incident is never reopened: a recurrence is a new incident.
- **Auto-resolve.** Each monitor run calls `resolve_absent(monitor, failing_subjects)`; incidents whose subject passes again are resolved.
- **Alerts.** Every incident alerts once. A **P1** that is still **open** alerts again after the repeat interval (e.g. 30 min) until someone acknowledges it; P2/P3 alert once. Acknowledged and resolved incidents never alert.
- **Audit.** Every change (raised, escalated, alerted, acknowledged, resolved, auto-resolved) is in the audit log, as `system` or `human:<name>`.

```bash
aecom incidents                                          # open + acknowledged (--all includes resolved)
aecom incidents ack inc_123 --by kaan --note "looking"   # stops repeat alerts; --by is required (exit 2 without it)
aecom incidents resolve inc_123 --by kaan --note "fixed"
```

## Sentinel
The Sentinel (`agents/sentinel/`, Argus, Sonnet) **diagnoses**; it is not the safety net. Detection, alerting and
containment (safe mode, retries, the reconciler) stay in deterministic code and never wait for it.

- **When it runs.** At the end of each monitor run (`aecom monitors`, scheduler every 5 min), after alerts and the
  safe-mode check, `monitors.diagnose_new` calls `sentinel.diagnose(incident_id)` for every incident that was alerted for
  the **first time** and is **P1 or P2**. It runs **at most once per incident**: the setting
  `sentinel_diagnosed:<incident id>` is written before the run, so a failing run isn't retried every 5 minutes (ask
  again by deleting that setting). Skipped when the agent is disabled (`agent_enabled:sentinel=off`), in scripted LLM
  mode without a sentinel script, or with `monitors.AUTO_DIAGNOSE = False`. A failing run is recorded in `ops_errors`
  (component `sentinel`) and never breaks the monitor run.
- **What it does.** Reads the incident, gathers evidence with read-only tools (open incidents, recent errors with
  masked, shortened tracebacks, the run timeline of a `run_id`, event bus status and dead letters, the `aecom doctor`
  checks, orders and inventory), follows the matching **runbook skill** (`runbook_<monitor>`, one per section of
  [Runbooks](Runbooks), seeded and granted to the Sentinel by `aecom init`, refreshed when the doc changes), then calls
  `submit_diagnosis`: summary, root-cause hypotheses ranked by evidence, the evidence lines and safe next actions are
  appended to the incident's **notes** (`sentinel: DIAGNOSIS ...`, masked) and audited (`incident` / `diagnosed`).
- **What it may do (K5, K9).** After diagnosing, and only where the matching runbook lists the action as safe, one
  remediation step at a time through the policy gate (`aecom.tools.remediation`, rules in `policy.yaml`, reserved for
  the Sentinel; denied under the kill switch, in safe mode and while agents are paused), recorded in the diagnosis'
  next actions:
  - `replay_dead_letter(event_id)` (T2): re-queues one dead letter and drains, like `aecom events retry <ID>`.
    `order.*`, `inventory.*` and `email.*` replay at once; any other topic becomes an approval for you
    (`rules.replay_dead_letter.topic_prefixes`).
  - `rerun_reconciler()` (T1): `aecom reconcile` plus a drain.
  - `resend_delivery_email(order_id)` (T2): only for a delivered order with `email_sent` false, through the normal
    delivery path (no new key); refused for already-emailed or undelivered orders.
  - `pause_agents(reason)` (T1): containment; deliveries keep running; the reason is audited.
  - `propose_correcting_entry(lines, memo, ref)` (T3): always an approval. When you approve it, a **new** balanced
    entry (to the cent, memo `correction: ...`) is posted with `ledger.post`; unbalanced lines fail and post nothing.
    Existing ledger rows are never updated or deleted.
- **What stays human.** Unpausing agents, leaving safe mode, the kill switch, acknowledging or resolving incidents,
  approving correcting entries and out-of-scope replays, refunds, keys, code and credentials. The Sentinel has no tool
  for any of them, and no `request_account_need` (needs go into the next actions).

## Issues and postmortems
Built in `aecom.incident_export` (K8, K10). Nothing here deploys, merges or changes an incident's status.

- **Issue (dry run by default).** `aecom incidents export ID` writes `reports/issues/<ID>.md` under the repo root and
  prints the path; it never touches the network. The issue has: severity, monitor, subject, detail, the timeline
  (first/last seen, alerts, acknowledged, resolved), the Sentinel's latest diagnosis from the incident notes, related
  `ops_errors` (of a referenced run or dead-lettered event, or from the hour before the incident; tracebacks shortened
  to the last lines, repo paths made relative), the run timeline of every `run_...` id the incident mentions, and a
  **Reproduce** section with a failing pytest skeleton (`fresh_company` fixture, the matching chaos injector and
  invariant: `undelivered_order` and `order_dead_letter` → `delivery_failures`, `unemailed_order` →
  `email_failures`, `duplicate_key` / `ledger_imbalance` → `duplicate_order_paid`, `stuck_run` / `errors` →
  `claude_down`; other monitors get a TODO).
- **Masking.** Everything is passed through `observability.mask` and then redacts licence keys and download tokens
  (by value, from the database), links and e-mail addresses. An issue may be public.
- **Live.** `aecom incidents export ID --live` also creates the issue via the GitHub REST API
  (`POST /repos/{GITHUB_REPO}/issues`, labels `incident` and the severity) when `GITHUB_REPO` (`owner/name`) and
  `GITHUB_TOKEN` are set; otherwise it refuses. The issue URL is added to the incident notes (`github: issue ...`)
  and audited (`incident` / `issue_exported`). A failed call is recorded in `ops_errors`; the dry-run file stays.
- **PR template.** `incident_export.pr_body(ID)` is the PR description for the Engineer agent or a human: problem,
  root cause (from the diagnosis), the failing test, a fix checklist, and "never deploys; a human merges".
- **Postmortem.** `aecom incidents postmortem ID [--by NAME]` stores a `postmortem` report (period `all`, a new
  version per call) for a **resolved P1** only; open/acknowledged or P2/P3 incidents are refused. Sections: What
  happened, Impact (orders affected and their order value in €, from the order ids the incident mentions), Why (the
  diagnosis' root cause), What changes (for a human to finish) and Regression test. The text passes the report
  number check: every figure is in the report data (impact, duration, alerts, and figures quoted from the incident
  record). Find it with `reports.list_reports("postmortem")` or the dashboard Reports page.

```bash
aecom incidents export inc_123            # dry run: prints reports/issues/inc_123.md
aecom incidents export inc_123 --live     # + GitHub issue (GITHUB_REPO, GITHUB_TOKEN)
aecom incidents postmortem inc_123 --by kaan
```

## Safe mode
If nobody reacts to a P1, the company gets safer, not riskier (K7).
- **When.** Each monitor run (`aecom monitors run`, scheduler every 5 min) checks for a P1 incident that has been **open** (not acknowledged) for more than 2 hours (`monitors.SAFE_MODE_AFTER`). If there is one and safe mode is off, it switches safe mode on, records why (`safe_mode_reason` names the incident) and since when, audits it and sends one extra P1 alert: "Safe mode is ON because ...". Acknowledging the P1 in time prevents it.
- **What it does.** The policy gate denies every side-effecting tool except `policy.yaml → defaults.continue_in_safe_mode`: deliveries (`deliver_order`, `add_license_keys`) and customer replies (`reply_email`, `send_email` to the customer of the case). Campaigns, vendors, expenses, refunds and price changes are denied. The kill switch still stops everything. See [Policy and Approvals](Policy-and-Approvals).
- **Leaving it.** Never automatic, not even when the incident resolves. A human acknowledges and fixes the incident, then leaves safe mode (audited with the name):

```bash
aecom safe-mode                     # on/off, since, reason
aecom safe-mode off --by kaan
```

Or `POST /safe-mode?on=false&by=kaan` (bearer token), or the **Leave safe mode** button on the dashboard Home page, which shows a red banner while safe mode is on. `GET /health` reports `safe_mode`.
