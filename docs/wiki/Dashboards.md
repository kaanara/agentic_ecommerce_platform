# Dashboards

```bash
uv run aecom dashboard        # Streamlit on http://localhost:8501
```
All pages read the company database directly. Approval buttons call `aecom.approvals` in-process, so the API doesn't need to be running.

## Login
Every page asks for a password before it shows any data or button, because the dashboard can approve spending and flip the kill switch.

```bash
uv run python -m aecom.dashboard_auth   # prompts twice (hidden), prints DASHBOARD_PASSWORD_HASH=scrypt$...
```
Paste the printed line into `.env` and restart the dashboard. Only the scrypt hash is stored; the password is never printed or saved.
- **Fails closed**: if `DASHBOARD_PASSWORD_HASH` is unset, the dashboard only explains how to set it.
- After 5 wrong attempts in a browser session, logins are refused for 60 s.
- **Log out** is in the sidebar. To change the password, generate a new line and restart.
- The login is a second line of defence, not a reason to expose the port. Streamlit serves plain HTTP, so keep it bound to localhost/the private network and reach it remotely only through Tailscale or an SSH tunnel (`ssh -L 8501:localhost:8501 pi`).

## Home
- Pending approvals, number of agent runs, orders, and the **kill switch** toggle.
- Recent decisions (ADRs) and recent events.

## Business
- KPI tiles: net revenue, gross margin, net profit, LTV, ROAS, AI cost per order.
- Range selector (7/30/90 days). Net revenue per day, orders per day, and spend per day by type (marketing, COGS, AI).
- P&L table, **OKRs with progress bars**, campaigns with ROAS, catalog and inventory (available keys), vendors and platforms (including which credential env var each needs).

## Agents
- **Running now** (top of the page): every agent run still marked `running`, with agent, age, steps, tool calls, cost, task and an **open** link to Run detail. It refreshes itself every 10 s (a Streamlit fragment; older Streamlit shows a Refresh button instead). Runs older than 30 min are marked 🔴 **stuck** and a red banner counts them. A stuck run usually means a crashed worker or a hung provider call: open it on Run detail, and use the kill switch on Home if it keeps spending.
- **Enabled agents** toggles.
- Tiles: runs, tool calls, LLM cost, escalations.
- LLM cost by agent; policy decisions by agent (allow / escalate / deny).
- Per-agent summary (tokens, cost, ok / not-ok), most used tools, and recent runs with the full task and output.
- Registry (tools, skills, MCP servers, who may use them), proposed lessons, and the **audit trail**.

## Approvals
- Enter a reviewer name, which is recorded as `decided_by`.
- Each card shows the tool, agent, risk tier badge, the reason it needs you, **editable JSON args**, and the task context.
- **Approve & execute** runs the exact call (with your edits) and shows the result. **Reject** records your note and notifies the agent through `approval.resolved`.
- A history table lists every decision.

## Run detail
What happened in one run, end to end, without LangSmith.
- Pick a run from the 100 most recent (time · agents · status), or open `/Run_Detail?run_id=<run id>` directly (the Agents page links here). The selected run is written back to the URL, so the link can be shared. An agent-run row id (`AgentRun.id`) works too.
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
Colours follow a fixed categorical order from the reference palette (`dashboard/_common.py → SERIES`), so a series keeps its colour as filters change. Charts are single-axis, and every chart has a table view on the same page.
