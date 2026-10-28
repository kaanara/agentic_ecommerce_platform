# Raspberry Pi

Decision (2026-10-28): the company runs on a Raspberry Pi 4 first. Once cumulative net revenue reaches about USD 20-30 it moves to a cloud server (see [Moving to the cloud](#moving-to-the-cloud)). Until then accounting stays the built-in ledger.

Files used here: `deploy/pi/aecom-scheduler.service`, `deploy/pi/aecom-api.service`, `deploy/pi/aecom-dashboard.service`, `deploy/pi/aecom-backup.service`, `deploy/pi/aecom-backup.timer`, `deploy/pi/journald-aecom.conf` and `scripts/update.sh`.

## 1. Hardware

- Raspberry Pi 4 with 4 GB RAM or more, 64-bit Raspberry Pi OS (Lite is enough).
- Keep `data/` (the SQLite database and backups) on a USB SSD, not on the SD card; SD cards wear out and corrupt on power loss. Mount the SSD (for example at `/mnt/ssd`) and either clone the project there or symlink `data/` to it.
- Use the official power supply (5.1 V / 3 A). Under-voltage is the most common cause of corruption and random reboots. A small UPS HAT is a good later upgrade.
- Wired Ethernet is more reliable than Wi-Fi.

## 2. Base setup

> **Shortcut:** `bash scripts/pi-bootstrap.sh --dry-run` shows, and without `--dry-run` performs, steps 2 and 3 for the user you are logged in as (packages, uv, dependencies, `.env` from the template, database, systemd units, log limits). It never starts the services, touches the firewall or changes sudo rules unless you add `--enable-services`, `--firewall` or `--allow-update-sudo`, and it never reads or prints `.env`. The manual steps below are what it does.

```bash
sudo adduser --disabled-password --gecos "" aecom
sudo timedatectl set-ntp true          # time sync matters for webhooks signatures and schedules
timedatectl status                     # "System clock synchronized: yes"
sudo timedatectl set-timezone Europe/Berlin   # or your own timezone
```

Install uv and the project as the `aecom` user:

```bash
sudo -iu aecom
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs /home/aecom/.local/bin/uv
git clone <your repository url> agentic_ecommerce_platform
cd agentic_ecommerce_platform
uv sync
cp -n .env.example .env && chmod 600 .env
nano .env                                           # fill in the keys; keep the file out of git and backups you share
uv run aecom init
```

Set `AECOM_API_TOKEN` and the dashboard password hash (`DASHBOARD_PASSWORD_HASH`, see [Dashboards](Dashboards)) in `.env`. Do not set `DASHBOARD_AUTH=off` on the Pi.

## 3. Services

The unit files contain two placeholders, `@USER@` and `@APP_DIR@`. Replace them with sed while installing (run as a sudo-capable user):

```bash
cd /home/aecom/agentic_ecommerce_platform/deploy/pi
for f in aecom-scheduler.service aecom-api.service aecom-dashboard.service aecom-backup.service aecom-backup.timer; do
  sed -e 's|@USER@|aecom|g' -e 's|@APP_DIR@|/home/aecom/agentic_ecommerce_platform|g' "$f" \
    | sudo tee "/etc/systemd/system/$f" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now aecom-scheduler.service aecom-api.service aecom-dashboard.service aecom-backup.timer
systemctl list-timers aecom-backup.timer
```

All three long-running units restart on failure (`Restart=always`, 5 s delay, and a start limit that is generous but not unlimited). The API listens on 127.0.0.1:8000 and the dashboard on 127.0.0.1:8501 only.

For `scripts/update.sh` to stop and start the services without a password, allow exactly that for the `aecom` user:

```bash
# exact commands only: a wildcard such as "aecom-*" would also match extra arguments (for example other services)
S=/usr/bin/systemctl
echo "aecom ALL=(root) NOPASSWD: $S start aecom-scheduler, $S stop aecom-scheduler, $S start aecom-api, $S stop aecom-api, $S start aecom-dashboard, $S stop aecom-dashboard" \
  > /tmp/aecom-update.sudoers && sudo visudo -cf /tmp/aecom-update.sudoers \
  && sudo install -m 440 -o root -g root /tmp/aecom-update.sudoers /etc/sudoers.d/aecom-update
```

### Journald limits

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp /home/aecom/agentic_ecommerce_platform/deploy/pi/journald-aecom.conf /etc/systemd/journald.conf.d/aecom.conf
sudo systemctl restart systemd-journald
```

This caps the log at 200 MB and one month. Read logs with `journalctl -u aecom-scheduler -f`.

## 4. Firewall

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow in on tailscale0      # only if you use Tailscale
sudo ufw enable
sudo ufw status verbose
```

No service port (8000, 8501) is opened: both bind to localhost.

## 5. Remote access to the dashboard

Never expose the dashboard to the internet. The dashboard login must be ON (`DASHBOARD_PASSWORD_HASH` set, `aecom doctor` is green) before anyone but you can reach it.

- Tailscale (recommended): `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`, then reach the Pi over the tailnet. Because the dashboard binds to 127.0.0.1, publish it to the tailnet with `sudo tailscale serve --bg 8501`, which stays private to your tailnet.
- SSH tunnel: `ssh -L 8501:127.0.0.1:8501 aecom@<pi-address>` and open http://localhost:8501.

## 6. Webhooks (Lemon Squeezy, AgentMail)

Payment and mail providers must reach `POST /webhooks/*` on the API. Use a Cloudflare Tunnel (no port forwarding) and limit it to those paths:

```yaml
# /etc/cloudflared/config.yml
tunnel: <tunnel id>
credentials-file: /etc/cloudflared/<tunnel id>.json
ingress:
  - hostname: hooks.example.com
    path: ^/webhooks/.*
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Everything outside `/webhooks/*` returns 404 at the edge, and the rest of the API is protected by `AECOM_API_TOKEN` anyway. Webhook signatures are still verified by the app. Put the public URLs (`https://hooks.example.com/webhooks/lemonsqueezy`, `.../webhooks/agentmail`) into the provider dashboards.

## 7. First-run checklist

1. `timedatectl` shows the clock synchronized.
2. `systemctl status aecom-scheduler aecom-api aecom-dashboard` are all active.
3. `systemctl list-timers aecom-backup.timer` shows the next run.
4. `uv run aecom backup` creates a backup, then `uv run aecom backup verify` passes.
5. `uv run aecom doctor` reports no FAIL (fix WARN items you care about).
6. `uv run aecom backup verify` once more after the first real data arrives.

## 8. Updating

```bash
cd /home/aecom/agentic_ecommerce_platform
scripts/update.sh --check     # dry run: shows incoming commits and whether the schema changes
scripts/update.sh             # real update
```

The update stops the three services for the whole window, takes a `pre-update` backup, pulls (fast-forward only), runs `uv sync` and `aecom init`, and runs `aecom doctor` while the services are still stopped; only then does it start them. Because nothing accepts orders until the checks pass, a rollback (previous commit, database restored from the backup if the schema revision changed) can never discard orders; the replaced database is kept as `data/aecom.db.before-restore-<timestamp>`. If the rollback itself fails (checkout, `uv sync`, restore or service start), the script says exactly which step failed, says so plainly when the database was NOT restored, leaves the services stopped, and exits non-zero. Once the services are running again a late doctor failure is only reported, never rolled back, because the database already holds new orders. Webhooks that arrive meanwhile are retried by Lemon Squeezy. `--force` allows a dirty tree; `--no-systemd` skips service control.

## 9. After a power cut

1. Let the Pi boot; the services and the backup timer start by themselves.
2. `uv run aecom doctor`. SQLite recovers from a clean journal; if doctor reports a database problem, stop the services and restore: `uv run aecom backup list`, `uv run aecom backup verify`, `uv run aecom backup restore <path> --yes`.
3. Check the time is synchronized (`timedatectl`) and that no webhook was missed (provider dashboards list failed deliveries; they can be redelivered).

## Moving to the cloud

Trigger: cumulative net revenue of about USD 20-30 (the revenue-milestone alert fires). Until then stay on the Pi.

What moves:
- `data/`: run `uv run aecom backup` on the Pi, copy the backup directory to the new server and run `uv run aecom backup restore <path> --yes` there, then `uv run aecom backup verify`.
- `.env`: take it from your password manager (never copy it through chat or git).
- The same systemd units (`deploy/pi/`), installed with the same sed step; adjust `@USER@` and `@APP_DIR@`.

What changes:
- Webhook URLs at Lemon Squeezy and AgentMail point at the new host.
- Optionally move from SQLite to Postgres later (`DATABASE_URL`).
- After the move, adopt an accounting tool: lexoffice or sevDesk.

Keep the Pi stopped (not deleted) for a week as a fallback.
