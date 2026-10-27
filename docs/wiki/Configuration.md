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
| `WEB_SEARCH_TOOL_TYPE` | `web_search_20260209` | Anthropic server-side web search version (dynamic filtering; current for the Claude 5.5 models — use `web_search_20250305` on older models or Vertex AI) |
| `WEB_SEARCH_MAX_USES` | `5` | Max `web_search` calls per API request (the API's `max_uses`) |
| `WEB_SEARCH_MAX_PER_RUN` | `15` | Max web searches per agent run. Once reached, the model is re-bound without `web_search` for the remaining steps (audited once as `web_search_cap`) |
| `WEB_SEARCH_PRICE_EUR` | `0.01` | Per-search fee added to `AgentRun.cost_eur` on top of tokens ($10 per 1,000 searches, converted) |
| `WEB_SEARCH_ALLOWED_DOMAINS` / `WEB_SEARCH_BLOCKED_DOMAINS` | `[]` | Optional JSON lists of hostnames; sent only when non-empty. The API takes one of the two — if both are set, allowed wins |
| `WEB_FETCH_TOOL_TYPE` | `web_fetch_20260209` | Anthropic server-side web fetch version (runs on Anthropic's side; no beta header) |
| `WEB_FETCH_MAX_USES` | `5` | Max `web_fetch` calls per request |
| `WEB_FETCH_MAX_CONTENT_TOKENS` | `8000` | Max tokens one fetched page may add to the context |
| `WEB_FETCH_PRICE_EUR` | `0` | Per-fetch fee. Web fetch has no per-use charge (fetched content is billed as input tokens); kept as a setting in case that changes |
| `WEB_FETCH_ALLOWED_DOMAINS` / `WEB_FETCH_BLOCKED_DOMAINS` | `[]` | As for web search. Counts per run come from the API's `usage.server_tool_use` (`web_search_requests` / `web_fetch_requests`) and are stored on `AgentRun.web_searches` / `web_fetches`; scripted mode binds no server tools, so both stay 0 |
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
