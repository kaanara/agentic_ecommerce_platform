# Getting Started

## Prerequisites
- [uv](https://docs.astral.sh/uv/). Python 3.12 is pinned in `.python-version`, and uv installs it for you.
- Optional: an Anthropic API key (for live mode) and Docker (for Postgres).

## 1. Install and seed
```bash
uv sync
cp .env.example .env
uv run aecom init --demo
```
`init` creates the tables, registers the built-in tools, seeds each agent's allowlist from its `TOOLS.md`, and creates the default OKRs. `--demo` also adds three demo products, a licence vendor, and 30 days of simulated orders.

## 2. Run the scenarios offline (no key, no cost)
```bash
uv run aecom scenario all --offline
```
Offline mode replays a **scripted** decision for every agent (`LLM_MODE=scripted`). The graphs, policy gate, ledger, event bus and dashboards all run for real. Only the model's choices are pre-recorded.

## 3. Open the dashboards
```bash
uv run aecom dashboard
```
Go to **Approvals**, enter your name, and approve or reject what the agents escalated. Approving runs the parked action.

## 4. Go live with Claude
In `.env`:
```
ANTHROPIC_API_KEY=...
LLM_MODE=anthropic
```
Then run, for example:
```bash
uv run aecom scenario product-launch
uv run aecom run "Find a second product line that raises LTV and plan its launch within €300"
uv run aecom select ad_platform "Acquire EU buyers of productivity licences at CAC < €15" -c "Google Ads" -c "Meta Ads"
```

## 5. Keep it running
```bash
uv run aecom scheduler --demo   # one simulated market day per minute, plus daily/weekly agent jobs
uv run aecom api                # REST API on :8000 (/docs)
```

## Tests
```bash
uv run pytest
```
Every test gets its own temporary SQLite database and scripted agents.
