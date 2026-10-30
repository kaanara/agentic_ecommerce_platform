# Operations and Troubleshooting

## Day-to-day commands
| Task | Command |
|---|---|
| Reset everything | `rm -rf data && uv run aecom init --demo` |
| Process queued events | `uv run aecom drain` |
| See failed events (dead letter) | `uv run aecom events dead` |
| Retry dead-lettered events after fixing the cause | `uv run aecom events retry` (all) or `uv run aecom events retry 121 122` |
| Fetch new customer email now | `uv run aecom agentmail poll` |
| Sync the store platform's catalog (Lemon Squeezy) | `uv run aecom store sync` |
| Simulate traffic | `uv run aecom simulate --days 3 --support-emails` |
| Emergency stop | `uv run aecom kill-switch on` |
| Pause agents (bookkeeping continues where `continue_while_paused` allows) | `uv run aecom agents-pause on` / `off` |
| Re-deliver paid orders whose `order.paid` was lost | `uv run aecom reconcile` (default `--older-than-minutes 15`) |
| Chaos check (fault injection, invariants) | `uv run aecom chaos all [--seed N]` (throwaway DB; `--use-configured-db` to opt out); exit 1 on any violation |
| Review the queue | `uv run aecom approvals` |
| Run the scheduler | `uv run aecom scheduler` (real cron) or `--demo` (compressed time) |
| API | `uv run aecom api` → http://127.0.0.1:8000/docs |

## Schema changes
The schema is owned by Alembic (`alembic.ini`, `migrations/versions/`). `uv run aecom init` upgrades the configured database to the latest revision; a database created before Alembic (no `alembic_version` table, for example an old `data/aecom.db`) gets the old add-missing-columns fix once and is then stamped at head.

To change a table: edit the model in `src/aecom/db.py`, then `uv run alembic revision --autogenerate -m "add foo to bar"` (it uses `DATABASE_URL`; point it at a database already at head). Review the generated file, put any data backfill in the same `upgrade()`, and run `uv run pytest tests/test_migrations.py`; it fails when models and migrations drift. Only the lead writes revisions, so the chain stays linear. Builders who need a schema change describe it in their patch notes.

## Health check: aecom doctor
`uv run aecom doctor` runs every health check and prints a report you can paste into an issue; `--json` gives the same as JSON. Exit code 1 if any check is `fail`. It runs before `init`, so it reports the database as it is and changes nothing. Each check is isolated: one that crashes shows up as `fail` ("check crashed: ...") and the others still run.

| Check | ok / warn / fail |
|---|---|
| `database` | `SELECT 1` works; shows the SQLite path (or the URL with the password hidden) |
| `schema` | fail if a table or column from the models is missing (run `aecom init`) |
| `kill_switch` | warn if the kill switch or agents pause is on |
| `event_bus` | warn if events wait for a retry; fail on any dead letter (`aecom events dead`) |
| `invariants` | fail if a chaos invariant is violated (names and counts only) |
| `approvals` | pending count and oldest age; warn if older than 24 h |
| `errors` | warn with the count of `ops_errors` rows from the last 24 h |
| `stuck_runs` | fail if an agent run is `running` for more than 30 min |
| `credentials` | names only, never values: warn if `ANTHROPIC_API_KEY` is missing with `LLM_MODE=anthropic`, `AGENTMAIL_API_KEY` with `AGENTMAIL_LIVE=true`, or `AECOM_API_TOKEN` / `DASHBOARD_PASSWORD_HASH` |
| `agentmail_live` | info: whether emails are really sent |
| `disk` | free space where the SQLite file lives: warn < 1 GB, fail < 200 MB |
| `backup` | skipped for now: backup age is not checked until hosting is decided |

The header shows the version (git short sha, or the package version outside a checkout).

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `ANTHROPIC_API_KEY missing` | Set the key in `.env`, or run with `--offline` / `LLM_MODE=scripted` |
| `database is locked` | Only one SQLite writer at a time. WAL and a 30 s busy timeout are on; for heavy concurrency use Postgres. Never publish events inside an open write session |
| Agent says `DENIED by policy` | Working as intended. Check the rule in `policy.yaml` and the agent's allowlist (`list_registry`) |
| Agent says `budget exhausted` | Daily € budget reached. Raise `budget_eur_per_day` in its `POLICY.md`, or wait until tomorrow |
| Tool call returns `ERROR: … multiple values for keyword argument` | A tool parameter collides with a LangChain keyword (`config`). Rename it |
| MCP tools missing | Server not `approved`+granted (or `sandboxed` for the Foreman), load timeout, or `MCP_AUTOLOAD=false`. Check the audit log for `mcp_load:<name>` |
| Emails not arriving | `AGENTMAIL_LIVE=false` keeps them in the outbox (Dashboard → Home/Agents, table `ops_outbox_emails`) |
| Approval shows `failed` | Underlying tool raised (for example `approve_tool` before `sandbox_tool`). The result column has the error |
| Dashboard says "Dashboard login is not configured" | The login is on but no password is set. Run `uv run aecom dashboard reset-password` on the machine (at least 12 characters) |
| `Port 8501 is not available` when starting the dashboard | The `aecom-dashboard` service (or a suspended `Ctrl-Z` job) already uses it. Use the service, or stop it with `sudo systemctl stop aecom-dashboard` / `kill %1` first |
| Chat page says chat is disabled | Chat needs the dashboard login on and a password set (Settings > Security) |
| Dashboard Agents page errors on Postgres | Replace `GROUP_CONCAT` with `string_agg(a.agent_id, ',')` in `pages/2_Agents.py` |

## Security checklist
- [ ] `.env` is never committed (it's in `.gitignore`).
- [ ] Use a dedicated AgentMail key; rotate any key that was shared.
- [ ] Review `policy.yaml` changes in PRs.
- [ ] Keep `AGENTMAIL_LIVE=false` with seed or demo data (fake addresses).
- [ ] External MCP clients only get read access unless policy explicitly opens a tool to `mcp_client`.
- [ ] Untrusted content (emails, web pages) is always wrapped in `<untrusted>` via `fence()`.
