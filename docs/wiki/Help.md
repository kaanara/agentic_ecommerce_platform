# Help

Quick answers for running aecom. Everything below is run from the project folder (`cd ~/agentic_ecommerce_platform` on the Pi).

## How do I find a command?

| You want to know | Run |
|---|---|
| Every command | `uv run aecom --help` |
| Options of one command | `uv run aecom <command> --help`, for example `uv run aecom backup --help` |
| What `python -m` modules do | Open the file: `src/aecom/<module>.py` starts with a description. `python -m aecom.dashboard_auth` is the old password helper (prints a hash for `.env`); prefer `aecom dashboard reset-password` |
| The full command list with examples | The top of `src/aecom/cli.py`, or [Operations and Troubleshooting](Operations-and-Troubleshooting) |
| Whether the machine is healthy | `uv run aecom doctor` |

## I want to...

| Goal | Command or place |
|---|---|
| Create or upgrade the database | `uv run aecom init` (safe to run again; also applies migrations) |
| Set or reset the dashboard password | `uv run aecom dashboard reset-password` (at least 12 characters), or Settings > Security |
| Open the dashboard | `uv run aecom dashboard` (http://localhost:8501), or the service on the Pi |
| Talk to an agent | Dashboard > Chat, Telegram, or `uv run aecom ask ceo "How are sales?"` / `uv run aecom chat finance` |
| See and decide approvals | Dashboard > Approvals, or `uv run aecom approvals` |
| Stop everything fast | `uv run aecom kill-switch on` (off again with `off`); `uv run aecom agents-pause on` pauses agent work only |
| Back up | Dashboard > Settings > Backups and updates, or `uv run aecom backup`; check with `uv run aecom backup verify` |
| See incidents | Dashboard > Health, or `uv run aecom incidents` |
| Run offline scenarios (no API key) | `uv run aecom scenario all --offline` |
| Check invariants under faults | `uv run aecom chaos all` |
| Update the Pi | `scripts/update.sh --check`, then `scripts/update.sh` |
| Change a setting or secret | Dashboard > Settings (secrets are written to `.env` only) |
| Run the company, sync the store, review memories, manage tool grants or vendor sign-ups | Dashboard > Operations (see [Dashboards](Dashboards)) |
| Turn backups on or off, set when they run, encrypt them | Dashboard > Settings > Backups and updates (see [Backups](Backups)) |
| Close a month, create the month-end report, export for the accountant | Dashboard > Accounting (see [Dashboards](Dashboards)) |
| Change the dashboard password | Dashboard > Settings > Security |

## Common problems

| You see | Do this |
|---|---|
| "Dashboard login is not configured" | `uv run aecom dashboard reset-password` |
| "Port 8501 is not available" | The service is already running (`systemctl status aecom-dashboard`), or an old run was only suspended with Ctrl-Z (`kill %1`) |
| Chat is disabled | Turn the login on and set a password in Settings > Security |
| Dashboard works only on the Pi itself | It listens on `127.0.0.1`. Use an SSH tunnel: `ssh -L 8501:127.0.0.1:8501 user@pi`, then open http://localhost:8501 |
| After a power cut | The services start by themselves (see [Raspberry Pi](Raspberry-Pi)); then run `uv run aecom doctor` |
| Anything else | [Operations and Troubleshooting](Operations-and-Troubleshooting) |

## Where things are documented

[Getting Started](Getting-Started), [Dashboards](Dashboards), [Chat and Channels](Chat-and-Channels), [Settings](Settings), [Raspberry Pi](Raspberry-Pi), [Backups](Backups), [Monitoring and Alerting](Monitoring-and-Alerting).
