# Operations and Troubleshooting

## Day-to-day commands
| Task | Command |
|---|---|
| Reset everything | `rm -rf data && uv run aecom init --demo` |
| Process queued events | `uv run aecom drain` |
| See failed events (dead letter) | `uv run aecom events dead` |
| Retry dead-lettered events after fixing the cause | `uv run aecom events retry` (all) or `uv run aecom events retry 121 122` |
| Fetch new customer email now | `uv run aecom agentmail poll` |
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
| Dashboard Agents page errors on Postgres | Replace `GROUP_CONCAT` with `string_agg(a.agent_id, ',')` in `pages/2_Agents.py` |

## Security checklist
- [ ] `.env` is never committed (it's in `.gitignore`).
- [ ] Use a dedicated AgentMail key; rotate any key that was shared.
- [ ] Review `policy.yaml` changes in PRs.
- [ ] Keep `AGENTMAIL_LIVE=false` with seed or demo data (fake addresses).
- [ ] External MCP clients only get read access unless policy explicitly opens a tool to `mcp_client`.
- [ ] Untrusted content (emails, web pages) is always wrapped in `<untrusted>` via `fence()`.
