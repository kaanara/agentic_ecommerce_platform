# Configuration

**Most settings are now UI-editable**: [Dashboard](Dashboards) > [Settings](Dashboards#settings) (reload applies live changes without restarting). Some critical settings (API key, database URL, LLM mode) still live in `.env` and `src/aecom/config.py` (pydantic-settings, read at startup). Names are case-insensitive.

## Live-editable settings (dashboard Settings page)

All changes to these settings bump `config_version` and are applied to the running process within 30 s (scheduler tick), per API request, or on dashboard refresh.

| Section | Variable | Type | Default | Notes |
|---|---|---|---|---|
| AI and models | `model_opus`, `model_sonnet`, `model_haiku` | str | `claude-opus-5-5`, etc. | Model ids |
| AI and models | `agent_max_steps` | int | 12 | Max model turns per run |
| AI and models | `price_opus`, `price_sonnet`, `price_haiku` | pair (input, output) | `[5,25]` etc. | USD per million tokens |
| AI and models | `usd_to_eur` | float | 0.92 | Cost conversion |
| AI and models | `web_search_max_uses`, `web_search_max_per_run` | int | 5, 15 | Rate limits |
| AI and models | `web_fetch_max_uses`, `web_fetch_max_content_tokens` | int | 5, 8000 | Rate limits |
| Budgets and costs | `marketing_budget_month_eur`, `vendor_budget_month_eur` | float | — | Daily budgets |
| Store and email | `store_adapter` | enum | `mock` | Storefront adapter |
| Store and email | `agentmail_live` | bool | false | Actually send email |
| Alerts and notifications | `alert_email` | str | — | P1/P2 alert recipient |
| Alerts and notifications | `ntfy_url`, `ntfy_token` | secret | — | ntfy push config |
| Alerts and notifications | `healthchecks_ping_url` | secret | — | External dead-man's switch |
| Alerts and notifications | `quiet_hours_start`, `quiet_hours_end` | time | — | Format "HH:MM" (suppresses non-high alerts) |
| Channels | `telegram_enabled` | bool | false | Enable Telegram channel |
| Channels | `telegram_bot_token` | secret | — | Telegram Bot API token |
| Chat | `chat_history_turns` | int | 5 | Conversation depth per page load |
| Schedules | `backup_enabled`, `backup_schedule` | bool, schedule | false, `1h` | Automatic backup job |
| Schedules | `policy_refresh_schedule` | schedule | `15m` | When to reload policies from disk |
| Backups and updates | `backup_dir`, `backup_target` | path | `data/backups` | Local and off-device copy targets |
| Security | `dashboard_auth_mode` | enum | `on` | Turn login on/off |
| Autonomy | Per-agent approval tiers K1–K7 | — | — | Set via [Autonomy](Dashboards#trust) settings |
| Advanced | `langsmith_tracing` | bool | false | Off by default (sends prompts/tools to LangSmith) |
| Advanced | `mcp_autoload` | bool | true | Load registry MCP servers |

## Environment variables and `.env`

Read at startup; changes require a restart.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for live mode |
| `LLM_MODE` | `anthropic` | `anthropic` or `scripted` (offline replay) |
| `DATABASE_URL` | `sqlite:///./data/aecom.db` | Or `postgresql://localhost:5432/aecom` |
| `BACKUP_DIR` | `data/backups` | Overridden by settings if set |
| `AGENTMAIL_API_KEY` | — | AgentMail key (dedicated, least privilege) |
| `AGENTMAIL_ORG_INBOX` | `enrgy@agentmail.to` | Sender inbox |
| `AGENTMAIL_LIVE` | `false` | Overridden by settings if set |
| `AECOM_API_TOKEN` | — | Bearer token for the HTTP API (all routes except `/health` and `/webhooks/agentmail`). Unset → 503 (fail closed). Read via `secrets`, not `config.py` |
| `WEB_SEARCH_TOOL_TYPE` | `web_search_20260209` | Anthropic server-side web search version (use `web_search_20250305` on older models or Vertex AI) |
| `WEB_FETCH_TOOL_TYPE` | `web_fetch_20260209` | Anthropic server-side web fetch version |
| `WEB_SEARCH_PRICE_EUR` | `0.01` | Per-search fee added to `AgentRun.cost_eur` ($10 per 1,000 searches, converted) |
| `WEB_FETCH_PRICE_EUR` | `0` | Per-fetch fee (no per-use charge; kept for future use) |
| `WEB_SEARCH_ALLOWED_DOMAINS`, `WEB_SEARCH_BLOCKED_DOMAINS` | `[]` | Optional JSON lists of hostnames |
| `WEB_FETCH_ALLOWED_DOMAINS`, `WEB_FETCH_BLOCKED_DOMAINS` | `[]` | As for web search |
| `MCP_LOAD_TIMEOUT_S` | `20` | Per-server load timeout |
| `LANGSMITH_TRACING` | `false` | Overridden by settings if set |
| `LANGSMITH_API_KEY` | — | LangSmith key (secret; exported to the environment automatically) |
| `LANGSMITH_PROJECT` | — | LangSmith project name; retention is set per project there |
| `DASHBOARD_PASSWORD_HASH` | — | Scrypt hash (generate with `uv run python -m aecom.dashboard_auth`); overridden by database if set |
| `DASHBOARD_AUTH` | `true` | Turns login on/off; overridden by database if set |
| `TELEGRAM_BOT_TOKEN` | — | Telegram Bot API token (secret); overridden by settings if set |
| `TELEGRAM_ENABLED` | `false` | Overridden by settings if set |

Policy and budgets live in `src/aecom/policy/policy.yaml` and each agent's `POLICY.md` (editable via dashboard Advanced tab).

## Postgres
```bash
docker compose up -d
uv sync --extra postgres
DATABASE_URL=postgresql://localhost:5432/aecom uv run aecom init --demo
```
