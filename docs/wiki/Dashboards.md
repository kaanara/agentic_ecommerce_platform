# Dashboards

```bash
uv run aecom dashboard        # Streamlit on http://localhost:8501
```
All pages read the company database directly. Approval buttons call `aecom.approvals` in-process, so the API doesn't need to be running.

## Home
- Pending approvals, number of agent runs, orders, and the **kill switch** toggle.
- Recent decisions (ADRs) and recent events.

## Business
- KPI tiles: net revenue, gross margin, net profit, LTV, ROAS, AI cost per order.
- Range selector (7/30/90 days). Net revenue per day, orders per day, and spend per day by type (marketing, COGS, AI).
- P&L table, **OKRs with progress bars**, campaigns with ROAS, catalog and inventory (available keys), vendors and platforms (including which credential env var each needs).

## Agents
- Tiles: runs, tool calls, LLM cost, escalations.
- LLM cost by agent; policy decisions by agent (allow / escalate / deny).
- Per-agent summary (tokens, cost, ok / not-ok), most used tools, and recent runs with the full task and output.
- Registry (tools, skills, MCP servers, who may use them), proposed lessons, and the **audit trail**.

## Approvals
- Enter a reviewer name, which is recorded as `decided_by`.
- Each card shows the tool, agent, risk tier badge, the reason it needs you, **editable JSON args**, and the task context.
- **Approve & execute** runs the exact call (with your edits) and shows the result. **Reject** records your note and notifies the agent through `approval.resolved`.
- A history table lists every decision.

## Chart conventions
Colours follow a fixed categorical order from the reference palette (`dashboard/_common.py → SERIES`), so a series keeps its colour as filters change. Charts are single-axis, and every chart has a table view on the same page.
