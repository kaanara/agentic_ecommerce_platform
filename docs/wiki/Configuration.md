# Configuration

All settings live in `src/aecom/config.py` (pydantic-settings). They're read from the environment or `.env`; names are case-insensitive.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for live mode |
| `LLM_MODE` | `anthropic` | `anthropic` or `scripted` (offline replay) |
| `MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU` | `claude-opus-5-5` / `claude-sonnet-5-5` / `claude-haiku-5-5` | Model id per tier |
| `AGENT_MAX_STEPS` | `12` | Max model turns per agent run |
| `PRICE_OPUS` / `PRICE_SONNET` / `PRICE_HAIKU` | `[5,25]` / `[3,15]` / `[1,5]` | USD per million tokens (input, output). **Set to your prices** |
| `USD_TO_EUR` | `0.92` | Cost conversion |
| `WEB_SEARCH_TOOL_TYPE` | `web_search_20250305` | Anthropic server tool version |
| `WEB_FETCH_TOOL_TYPE` | `web_fetch_20260209` | Anthropic server-side web fetch version (runs on Anthropic's side; no beta header) |
| `WEB_FETCH_MAX_USES` | `5` | Max `web_fetch` calls per request |
| `DATABASE_URL` | `sqlite:///./data/aecom.db` | Or `postgresql+psycopg://aecom:aecom@localhost:5432/aecom` |
| `AGENTMAIL_API_KEY` | — | AgentMail key (dedicated, least privilege) |
| `AGENTMAIL_ORG_INBOX` | `enrgy@agentmail.to` | Sender inbox |
| `AGENTMAIL_LIVE` | `false` | Actually send email |
| `AECOM_API_TOKEN` | — | Bearer token for the HTTP API (all routes except `/health` and `/webhooks/agentmail`). Unset → 503 (fail closed). Read via `secrets`, not `config.py` |
| `STORE_ADAPTER` | `mock` | Storefront adapter |
| `MCP_AUTOLOAD` | `true` | Load registry MCP servers into agents |
| `MCP_LOAD_TIMEOUT_S` | `20` | Per-server load timeout |
| `LANGSMITH_*` | — | Tracing (exported to the environment automatically) |

Runtime settings (`ops_settings`, changeable without a restart): `kill_switch`, `marketing_budget_month_eur`, `vendor_budget_month_eur`.

Policy and budgets: `src/aecom/policy/policy.yaml` and each agent's `POLICY.md`.

## Postgres
```bash
docker compose up -d
uv sync --extra postgres
DATABASE_URL=postgresql+psycopg://aecom:aecom@localhost:5432/aecom uv run aecom init --demo
```
