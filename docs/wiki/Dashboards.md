# Dashboards

```bash
uv run aecom dashboard        # Streamlit on http://localhost:8501
```
All pages read the company database directly. Approval buttons call `aecom.approvals` in-process, so the API doesn't need to be running.

## Login
Every page asks for a password before it shows any data or button, because the dashboard can approve spending and flip the kill switch.

```bash
uv run aecom dashboard reset-password   # on the machine: prompts twice (hidden), at least 12 characters, turns the login on
```
The password is stored as a scrypt hash in the database (never printed or saved in clear). You can also set, change or turn off the login in **Settings > Security** (changing it needs the current password). The older `.env` route still works until you save from the UI: `uv run python -m aecom.dashboard_auth` prints a `DASHBOARD_PASSWORD_HASH=` line for `.env`; precedence is database, then `.env`.
- **Fails closed**: with the login on but no password set, the dashboard shows "Dashboard login is not configured" and nothing else. Run `reset-password` above.
- After 5 wrong attempts in a browser session, logins are refused for 60 s.
- **Log out** is in the sidebar. Sessions end after the idle timeout (12 h by default, a setting).
- The login is a second line of defence, not a reason to expose the port. Streamlit serves plain HTTP, so keep it bound to localhost/the private network and reach it remotely only through Tailscale or an SSH tunnel (`ssh -L 8501:localhost:8501 pi`).
- **Reviewer name**: enter your name in the sidebar text box for the audit log (recorded on every approval, switch or settings change).

## Overview
Home page: pending approvals, agent runs count, orders, and the **kill switch** toggle. Recent decisions and events. Inbox of dashboard-only notifications (not routed to Telegram or ntfy).

## Chat
Direct conversation with any available agent. Messages and attachments are stored and masked in the conversation history. Requires dashboard login to be on; paired Telegram/WhatsApp chats skip the login check.

## Approvals
- Pick a pending approval (filter by risk tier or agent).
- Each card shows the tool, agent, risk tier badge, the reason, **editable JSON args**, and the task context.
- **Approve & execute** runs the exact call (with your edits) and shows the result. **Reject** records your note and notifies the agent.
- Tier-capped approval: paired Telegram chats can only approve certain risk tiers (default T1); raise the cap in [Settings](Settings) > Channels.
- A history table lists every decision.

## Business
- KPI tiles: net revenue, gross margin, net profit, LTV, ROAS, AI cost per order.
- Range selector (7/30/90 days). Net revenue per day, orders per day, and spend per day by type (marketing, COGS, AI).
- P&L table, **OKRs with progress bars**, campaigns with ROAS, catalog and inventory (available keys), vendors and platforms (including which credential env var each needs).

## Agents
- **Running now** (top of the page): every agent run still marked `running`, with agent, age, steps, tool calls, cost, task and an **open** link to Run detail. It refreshes itself every 10 s (a Streamlit fragment; older Streamlit shows a Refresh button instead). Runs older than 30 min are marked 🔴 **stuck** and a red banner counts them. A stuck run usually means a crashed worker or a hung provider call: open it on Run detail, and use the kill switch on Overview if it keeps spending.
- **Enabled agents** toggles (persisted in settings).
- Tiles: runs, tool calls, LLM cost, escalations.
- LLM cost by agent; policy decisions by agent (allow / escalate / deny).
- Per-agent summary (tokens, cost, ok / not-ok), most used tools, and recent runs with the full task and output.
- Registry (tools, skills, MCP servers, who may use them), proposed lessons, and the **audit trail**.

## Costs
- Agent budgets per day (EUR), spend today, remaining.
- LLM cost breakdown: per model (Opus/Sonnet/Haiku), per agent, over time.
- Cost per order, weekly trends, top-spending runs.

## Reports
- Ledger: all transactions, balances and reconciliation status.
- Orders: sales pipeline, status distribution, revenue by source.
- Customers and campaigns: acquisition cost, lifetime value, retention.
- Incidents: open, resolved, by severity and age.

## Trust
- **Kill switch**: on/off, and when it was last changed and why.
- **Safe mode**: when activated, only customer-initiated delivery and support flows run (all other agents paused).
- **Autonomy**: which agent runs can execute without approval (approval gate levels K1–K7; see [Policy and Approvals](Policy-and-Approvals) for definitions).
- **Consent tracking**: customer withdrawal-right decisions, GDPR requests.
- **Audit trail**: who changed settings, who approved what, policy decisions and errors. Searchable by date, agent, user.

## Health
- Current incident summary: open P1 and P2 incidents, count, age.
- Monitors state (via scheduler's latest run): undelivered orders, unemailed orders, stuck runs, errors, kill switch, backup staleness, event backlog, approvals aging, etc.
- `/health` endpoint status (reachable, last tick, open P1s).
- Safe mode timeline: when it was on and why.

## Operations
**Govern > Operations** has what used to be CLI-only. Enter your name in the sidebar first; every action is recorded in the audit log.
- **Run and maintenance:** start a company run from a directive (`aecom run`), sync the store catalog (`aecom store sync`), reconcile stuck paid orders (`aecom reconcile`), run the monitors now (`aecom monitors run`) and poll the inbox (`aecom agentmail poll`). These are queued as jobs and run by the scheduler within about 30 seconds; refresh to see the result. A warning shows when `LLM_MODE=anthropic`, because a company run then spends real money.
- **Memory review:** approve or reject shared agent memories (`aecom memory`); only approved ones are recalled by agents. Provenance and taint are shown.
- **Tool grants:** see each agent's tool allowlist and revoke a tool (`aecom registry revoke`), behind a confirmation.
- **Vendor provisioning:** the checklist for each vendor waiting on you: mark steps done, snooze, and confirm that it works (`aecom provisioning`). Credentials are never shown.
- **Incident reports:** preview an incident's issue text, write the issue file, draft a P1 postmortem (`aecom incidents export|postmortem`). Creating the issue on GitHub needs a configured token, your password again and typing the incident id.

Still CLI-only, on purpose: `aecom scenario`, `aecom simulate`, `aecom chaos` (developer tools that create test data) and `aecom init` (run on every update).

## Accounting
**Govern > Accounting** (enter your name in the sidebar first):
- **Months:** every month with ledger lines, its status and a fingerprint. **Close month** (a confirmation; months close in order, only once the month is over and the books balance) stops any posting into it and stores a fingerprint of its ledger lines. **Verify all** recomputes the fingerprints and flags a month whose lines changed. **Reopen newest** needs your password again and a reason. Only humans can do this: no agent tool exists for it. CLI: `aecom period list|close|reopen|verify`.
- **Month-end report:** P&L, cash, VAT per country and rate, budget against spend and checks, as an immutable report with downloads. Payout reconciliation is listed as open until that feature exists. CLI: `aecom month-end YYYY-MM`.
- **Accountant export:** a DATEV draft or a plain CSV journal for a finished month, with warnings for expenses that have no receipt. Account numbers are defaults for you to confirm with your accountant. See [Accounting Export](Accounting-Export). CLI: `aecom export datev|csv YYYY-MM`.
- **Decision review:** each quarter, decisions older than 60 days that are still accepted come up for a look back (delivered, partly, not delivered, superseded) with the facts that help you judge. A weekly reminder is sent once per quarter. CLI: `aecom decisions`.

The ledger is append-only: a mistake is fixed with a new balancing entry, never by editing a row.

## Settings
- **Security**: toggle dashboard login on/off, set password.
- **AI and models**: Anthropic API key, model ids, max steps per run, token prices, web search config.
- **Budgets and costs**: daily budgets per agent (EUR), cost conversion rate.
- **Store and email**: store adapter, AgentMail inbox and live mode.
- **Alerts and notifications**: ntfy URL and token, email recipient, healthchecks.io heartbeat, quiet hours.
- **Channels**: Telegram bot token, enable/disable, pairing codes; WhatsApp (when configured).
- **Chat**: history depth per conversation.
- **Schedules**: monitor run frequency, policy refresh, other job intervals.
- **Backups and updates**: backup directory and off-device target (local path, rsync over ssh, or rclone), retention policy.
- **Autonomy**: override approval gates per agent (K1–K7) to allow higher autonomy without escalation (requires re-confirmation of password).
- **Advanced**: LangSmith tracing (off by default), MCP autoload, web fetch config, policy edits, agent persona edits.

## Run detail
What happened in one run, end to end, without LangSmith. Reached by a link from Agents page or via `?run_id=<id>`.
- Pick a run from the 100 most recent (time · agents · status), or paste an agent-run row id (`AgentRun.id`).
- Tiles: status (`ok`, `running`, `error`, `vetoed`, `unknown`), agents, tool calls, approvals, errors, LLM cost; below them the agents and tools used and the start/end time.
- **Timeline**: one table in time order, filterable by kind: `run_start`/`run_end` (model, task, status, steps, cost, output), `tool_call` (tool, policy decision, failed flag, shortened args/result when recorded), `approval_created`/`approval_decided` (tool, tier, reason, status, who decided), `audit`, `error`, `decision` (ADRs recorded by the run's agents while it ran; matched by time, as decisions carry no run id) and `incident` (incidents whose subject or detail mentions the run id). `ref` is the source row id.
- **Approvals** and **Errors** expanders with the reason, note, result, message and traceback. A link leads to the Approvals page when something is still pending.
- All text is masked again on display (`aecom.observability.mask`), on top of the masking done when it was recorded.

The data comes from `aecom.run_detail` (`running_now()`, `timeline(run_id)`, `summary(run_id)`, `recent_run_ids()`), plain functions with no Streamlit dependency, so the Sentinel and scripts can use the same view:
```python
from aecom import run_detail
run_detail.running_now()            # [{run_id, agent_id, age_s, stuck, ...}] - stuck after 30 min by default
run_detail.timeline("run_...")      # [{ts, kind, actor, summary, ref}] in time order
run_detail.summary("run_...")       # {agents, tools_used, tool_calls, approvals, errors, cost_eur, status, ...}
```

## Chart conventions
Colours follow a fixed categorical order from the reference palette, so a series keeps its colour as filters change. Charts are single-axis, and every chart has a table view on the same page.
