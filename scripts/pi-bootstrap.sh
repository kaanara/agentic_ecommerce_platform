#!/usr/bin/env bash
# Prepare a Raspberry Pi (64-bit Raspberry Pi OS) or any Debian-based machine for aecom.
#
# Run it ON THE PI, as your normal user (not root), in the project directory or with --app-dir/--repo:
#     bash scripts/pi-bootstrap.sh --dry-run     # prints what it would do, changes nothing
#     bash scripts/pi-bootstrap.sh               # does it, asking before anything sensitive
#
# It follows docs/wiki/Raspberry-Pi.md and is safe to run again. By default it does NOT start the services,
# does NOT touch the firewall and does NOT change sudo rules: those are opt-in flags, because services should
# only start after .env has been filled in. It never reads or prints .env, and never creates accounts.
set -euo pipefail

DRY=0; YES=0; REPO=""; BRANCH="feat/agentic-ecommerce-poc"; APP_DIR="$HOME/agentic_ecommerce_platform"
FIREWALL=0; ENABLE=0; SUDOERS=0
UNITS=(aecom-scheduler.service aecom-api.service aecom-dashboard.service aecom-backup.service aecom-backup.timer)
SERVICES=(aecom-scheduler aecom-api aecom-dashboard)

usage() {
  cat <<'EOF'
usage: pi-bootstrap.sh [options]
  --dry-run            show every step, change nothing
  --yes                do not ask for confirmation (still skips opt-in steps unless their flag is given)
  --app-dir DIR        where the project lives or should be cloned (default: ~/agentic_ecommerce_platform)
  --repo URL           clone this repository into --app-dir when it is not there yet
  --branch NAME        branch to clone (default: feat/agentic-ecommerce-poc)
  --allow-update-sudo  let this user start/stop exactly the three aecom services without a password (for update.sh)
  --firewall           ufw: deny incoming, allow outgoing, allow ssh (CAN LOCK YOU OUT if ssh is on another port)
  --enable-services    enable and start the scheduler, API, dashboard and the backup timer (after .env is filled in!)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes) YES=1 ;;
    --app-dir) APP_DIR="${2:?--app-dir needs a value}"; shift ;;
    --repo) REPO="${2:?--repo needs a value}"; shift ;;
    --branch) BRANCH="${2:?--branch needs a value}"; shift ;;
    --allow-update-sudo) SUDOERS=1 ;;
    --firewall) FIREWALL=1 ;;
    --enable-services) ENABLE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf '    ! %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
run()  { printf '    $ %s\n' "$*"; if [ "$DRY" -eq 0 ]; then "$@"; fi; }
confirm() {
  [ "$YES" -eq 1 ] && return 0
  [ "$DRY" -eq 1 ] && return 0
  local a; read -r -p "    $1 [y/N] " a
  [ "$a" = "y" ] || [ "$a" = "Y" ]
}

[ "$DRY" -eq 1 ] && say "DRY RUN: nothing below is executed"

# --------------------------------------------------------------------------------------------- 0. preflight
say "0/8 preflight"
[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root (it uses sudo only where needed)"
if [ -r /etc/os-release ] && grep -qiE '^(ID|ID_LIKE)=.*(debian|raspbian|ubuntu)' /etc/os-release; then
  echo "    OS: $(. /etc/os-release; echo "${PRETTY_NAME:-unknown}") on $(uname -m)"
else
  if [ "$DRY" -eq 1 ]; then warn "not a Debian-based Linux (fine for a dry run)"; else die "needs Debian, Raspberry Pi OS or Ubuntu"; fi
fi
case "$(uname -m)" in aarch64|arm64|x86_64) ;; *) warn "unusual CPU $(uname -m): Raspberry Pi OS should be the 64-bit image" ;; esac
command -v sudo >/dev/null 2>&1 || { [ "$DRY" -eq 1 ] || die "sudo is required"; }
free_kb=$(df -Pk "$HOME" | awk 'NR==2 {print $4}')
[ "${free_kb:-0}" -ge 2000000 ] || warn "less than 2 GB free in $HOME (${free_kb:-?} KB); an SSD is recommended, see the Pi guide"
if command -v timedatectl >/dev/null 2>&1; then
  [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo no)" = "yes" ] \
    || warn "clock is not NTP-synchronised: webhook signatures and schedules depend on it (sudo timedatectl set-ntp true)"
fi
if [ "$DRY" -eq 0 ]; then sudo -v || die "sudo did not authenticate"; fi

# --------------------------------------------------------------------------------------------- 1. packages
say "1/8 system packages"
run sudo apt-get update
run sudo apt-get install -y git curl sqlite3 ca-certificates

# --------------------------------------------------------------------------------------------- 2. uv
say "2/8 uv (Python and dependency manager)"
if [ -x "$HOME/.local/bin/uv" ] || command -v uv >/dev/null 2>&1; then
  echo "    uv is already installed"
elif confirm "install uv with its official installer (https://astral.sh/uv/install.sh)?"; then
  printf '    $ curl -LsSf https://astral.sh/uv/install.sh | sh\n'
  if [ "$DRY" -eq 0 ]; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi
else
  die "uv is required"
fi
UV="$HOME/.local/bin/uv"; [ -x "$UV" ] || UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

# --------------------------------------------------------------------------------------------- 3. code
say "3/8 project code in $APP_DIR"
CODE=1
if [ -f "$APP_DIR/pyproject.toml" ]; then
  echo "    found"
elif [ -n "$REPO" ]; then
  confirm "clone $REPO (branch $BRANCH) into $APP_DIR?" && run git clone --branch "$BRANCH" "$REPO" "$APP_DIR"
else
  CODE=0
  warn "no project at $APP_DIR. Copy it there first, for example from your Mac:"
  warn "    rsync -a --exclude .venv --exclude data --exclude .env --exclude .claude/worktrees ./ $USER@<this-pi>:$(basename "$APP_DIR")/"
  warn "or push the branch and re-run with --repo https://github.com/<you>/agentic_ecommerce_platform.git"
  [ "$DRY" -eq 1 ] || die "project code missing"
fi

[ "$DRY" -eq 1 ] || [ -f "$APP_DIR/pyproject.toml" ] || die "no project at $APP_DIR (nothing was cloned)"

# --------------------------------------------------------------------------------------------- 4-5. install
if [ "$CODE" -eq 1 ] && { [ -d "$APP_DIR" ] || [ "$DRY" -eq 0 ]; }; then
  say "4/8 dependencies"
  ( cd "$APP_DIR" && run "$UV" sync )

  say "5/8 .env (created from the template if missing; its contents are never read or printed)"
  if [ -f "$APP_DIR/.env" ]; then echo "    .env exists: left untouched"; else ( cd "$APP_DIR" && run cp .env.example .env ); fi
  ( cd "$APP_DIR" && run chmod 600 .env )

  say "6/8 database and checks (no demo data)"
  ( cd "$APP_DIR" && run "$UV" run aecom init )
fi

# --------------------------------------------------------------------------------------------- 7. services
say "7/8 systemd units and log limits"
install_unit() {  # name -> /etc/systemd/system/name with @USER@ and @APP_DIR@ replaced
  local f="$APP_DIR/deploy/pi/$1"
  printf '    $ sed ... %s | sudo tee /etc/systemd/system/%s\n' "$f" "$1"
  [ "$DRY" -eq 1 ] || { sed -e "s|@USER@|$USER|g" -e "s|@APP_DIR@|$APP_DIR|g" "$f" | sudo tee "/etc/systemd/system/$1" >/dev/null; }
}
if [ "$CODE" -eq 1 ]; then
  for u in "${UNITS[@]}"; do install_unit "$u"; done
  run sudo mkdir -p /etc/systemd/journald.conf.d
  run sudo cp "$APP_DIR/deploy/pi/journald-aecom.conf" /etc/systemd/journald.conf.d/aecom.conf
  run sudo systemctl restart systemd-journald
  run sudo systemctl daemon-reload
fi

if [ "$SUDOERS" -eq 1 ]; then
  say "   sudo rule for update.sh: exactly start/stop of ${SERVICES[*]} for $USER, nothing else"
  SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"; SYSTEMCTL="$(readlink -f "$SYSTEMCTL")"
  rules=(); for s in "${SERVICES[@]}"; do rules+=("$SYSTEMCTL start $s" "$SYSTEMCTL stop $s"); done
  line="$USER ALL=(root) NOPASSWD: $(IFS=,; echo "${rules[*]}" | sed 's/,/, /g')"
  echo "    $line"
  if [ "$DRY" -eq 0 ] && confirm "install this rule into /etc/sudoers.d/aecom-update (validated with visudo first)?"; then
    tmp="$(mktemp)"; echo "$line" > "$tmp"
    sudo visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "generated sudoers rule did not validate; nothing installed"; }
    sudo install -m 440 -o root -g root "$tmp" /etc/sudoers.d/aecom-update; rm -f "$tmp"
  fi
fi

if [ "$FIREWALL" -eq 1 ]; then
  say "   firewall (ufw): deny incoming, allow outgoing, allow ssh on 22/tcp"
  warn "if you reach this machine over ssh on another port, this locks you out. Console access is the way back."
  if confirm "enable the firewall now?"; then
    run sudo apt-get install -y ufw
    run sudo ufw default deny incoming
    run sudo ufw default allow outgoing
    run sudo ufw allow 22/tcp
    run sudo ufw --force enable
  fi
fi

if [ "$ENABLE" -eq 1 ] && [ "$CODE" -eq 1 ]; then
  say "   enabling and starting the services"
  warn "make sure .env is filled in (keys, AECOM_API_TOKEN, dashboard password) before continuing"
  if confirm "start the services now?"; then
    run sudo systemctl enable --now aecom-scheduler.service aecom-api.service aecom-dashboard.service aecom-backup.timer
  fi
fi

# --------------------------------------------------------------------------------------------- 8. summary
say "8/8 done. What is left for you"
cat <<EOF
    1. Fill in the secrets (the file stays on this machine):     nano $APP_DIR/.env
         ANTHROPIC_API_KEY, AGENTMAIL_API_KEY, AECOM_API_TOKEN, and DASHBOARD_PASSWORD_HASH
         (create the hash with:  cd $APP_DIR && $UV run python -m aecom.dashboard_auth ).
         Do NOT keep DASHBOARD_AUTH=off on a machine other people can reach.
    2. Check the setup:                                           cd $APP_DIR && $UV run aecom doctor
    3. Take and verify a first backup:                            $UV run aecom backup && $UV run aecom backup verify
    4. Start the services (once .env is ready):                   bash scripts/pi-bootstrap.sh --enable-services
    5. Open the dashboard from your computer (it listens on 127.0.0.1 only):
                                                                  ssh -L 8501:localhost:8501 $USER@<this-pi>   then http://localhost:8501
    6. Updating later:                                            bash scripts/update.sh --check    then    bash scripts/update.sh
EOF
