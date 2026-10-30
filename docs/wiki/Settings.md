# Settings

Govern > Settings is the one place to change options. Search at the top, or open a tab. Most changes apply live; a few show a "restart needed" banner with the command to run.

**Tabs:** AI and models, Budgets and costs, Store and email, Alerts and notifications, Channels, Chat, Schedules, Security, Policy, Agents, Backups and updates, Autonomy, Advanced, History, Export / Import.

## Where a value comes from

Every field shows its source and has "Reset to default".

| Layer | Used for | Order |
|---|---|---|
| UI override (`ops_settings`, key `config:<name>`) | non-secret options | 1st |
| `.env` | non-secret options and secrets | 2nd |
| Built-in default | everything | 3rd |

- **Secrets** (API keys, tokens, webhook secrets) are written to `.env` only, never to the database, because backups contain the database but not `.env`. The page shows only "set / not set", the last 4 characters and when it changed. Values are never shown again.
- **Read-only items** (`DATABASE_URL`, directory paths) are listed under Advanced; changing them from the running app would cut it off from its own data.
- **Autonomy stages** are changed on the Trust page, which checks the evidence. Settings only links there.
- Each save bumps `config_version`. The scheduler, API and dashboard check it and reload (the scheduler within about 30 s, rescheduling its jobs).

## Who can change what

Only logged-in owners. **Critical** items ask for the dashboard password again before saving: secrets, the login settings, `LLM_MODE`, `AGENTMAIL_LIVE`, the store adapter, and policy changes that loosen a rule. These changes are announced through the owner notification router. No agent tool can read or write settings, login or channel pairing.

## Security

- **Login off and no password:** set a password (at least 12 characters, twice). Saving turns the login on. Anyone who can reach the dashboard can do this, which is acceptable only because it listens on `127.0.0.1` behind an SSH tunnel.
- **Login on:** changing the password needs the current one; turning the login off needs the current password plus a confirmation.
- An idle timeout setting ends sessions (12 h by default).
- Chat and channel pairing require the login to be on.
- **Forgot the password?** On the machine, run `uv run aecom dashboard reset-password`.

Precedence for login mode and password: database, then `.env`, then fail closed.

## Policy and Agents

- **Policy:** edits are saved as versions in `ops_policy_versions` and validated by the policy loader first. You see a diff, can roll back to any version, and loosening a rule (lower tier, higher limit, any T4 change) needs the password again. The shipped `policy.yaml` is the fallback.
- **Agents:** personas and `company/` documents are edited as overrides in `ops_persona_overrides`, so tracked files are never edited on the Pi and `git pull` never conflicts. You get a preview, a diff, "Reset to file version" and a warning if the file changed underneath. Changes to `TOOLS.md` go through the additive allowlist resync.

## Schedules and backups

Each job's interval (`30s`, `15m`, `6h`) or cron (`cron:M H DOM MON DOW`, with 0 or 7 as Sunday and 1 as Monday) is a setting. The Backups and updates tab holds the backup schedule, retention, verify-after, and the off-device target; the buttons for Back up now, Verify, Restore and Download are described in [Backups](Backups).

## History, export and import

History lists every change with old and new values (secrets show only "changed") and who made it. Export writes the non-secret settings as YAML so you can copy them to another machine; secrets and critical settings are never exported.
