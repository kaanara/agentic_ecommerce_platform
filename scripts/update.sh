#!/usr/bin/env bash
# Safe updater for a Raspberry Pi / server install. See docs/wiki/Raspberry-Pi.md.
#   scripts/update.sh --check      dry run: fetch, list incoming commits, change nothing
#   scripts/update.sh              update (services stopped for the whole window)
#   --force                        allow a dirty working tree
#   --no-systemd                   do not stop/start the aecom-* services
# On any failure after the backup the previous commit (and database, if the schema changed) is restored.
set -euo pipefail

CHECK=0
FORCE=0
SYSTEMD=1
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --force) FORCE=1 ;;
    --no-systemd) SYSTEMD=0 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
SERVICES=(aecom-scheduler aecom-api aecom-dashboard)

say() { printf '[update] %s\n' "$*"; }

svc() { # svc start|stop
  [ "$SYSTEMD" -eq 1 ] || return 0
  local s rc=0
  for s in "${SERVICES[@]}"; do
    if [ "$(id -u)" -eq 0 ]; then systemctl "$1" "$s" || rc=1; else sudo -n systemctl "$1" "$s" || rc=1; fi
  done
  return $rc
}

# ---------------------------------------------------------------- dry run
if [ "$CHECK" -eq 1 ]; then
  git fetch --quiet
  say "current commit: $(git rev-parse --short HEAD)"
  if ! git rev-parse --verify --quiet '@{u}' >/dev/null; then
    say "no upstream branch configured; nothing to compare"
    exit 0
  fi
  INCOMING=$(git rev-list 'HEAD..@{u}' | wc -l | tr -d ' ')
  if [ "$INCOMING" -eq 0 ]; then
    say "up to date"
    exit 0
  fi
  say "$INCOMING incoming commit(s):"
  git log --oneline 'HEAD..@{u}'
  if git diff --name-only 'HEAD..@{u}' | grep -qE '^(migrations/|src/aecom/db\.py$)'; then
    say "schema change: incoming commits touch migrations/ or src/aecom/db.py (the database is migrated; a backup is taken first)"
  else
    say "no schema change (migrations/ and src/aecom/db.py untouched)"
  fi
  say "dry run only: nothing was changed"
  exit 0
fi

# ---------------------------------------------------------------- real update
if [ "$FORCE" -eq 0 ] && [ -n "$(git status --porcelain)" ]; then
  say "working tree is dirty; commit/stash your changes or pass --force" >&2
  exit 1
fi

alembic_rev() { uv run alembic current 2>/dev/null | grep -oE '^[0-9a-zA-Z_]+' | tail -n 1 || true; }

FROM_COMMIT=$(git rev-parse HEAD)
FROM_REV=$(alembic_rev)
BACKUP_PATH=""
DOCTOR="not run"
STAGE="start"

rollback() {
  set +e
  trap - ERR
  local problems=() restored="n/a (schema unchanged)"
  say "FAILED during: $STAGE - rolling back" >&2
  svc stop || problems+=("could not stop all services")
  git checkout --quiet "$FROM_COMMIT" || problems+=("checkout of ${FROM_COMMIT:0:7} FAILED")
  uv sync || problems+=("uv sync FAILED")
  TO_REV=$(alembic_rev)
  if [ -n "$BACKUP_PATH" ] && [ "$TO_REV" != "$FROM_REV" ]; then
    say "schema revision changed ($FROM_REV -> $TO_REV): restoring database from $BACKUP_PATH" >&2
    if uv run aecom backup restore "$BACKUP_PATH" --yes; then
      restored="restored (the replaced DB is kept as data/aecom.db.before-restore-<timestamp>)"
    else
      restored="NOT RESTORED"
      problems+=("database restore FAILED: the database was NOT restored from $BACKUP_PATH")
    fi
  fi
  if [ "${#problems[@]}" -gt 0 ]; then
    say "ROLLBACK INCOMPLETE; services left STOPPED. Problems:" >&2
    printf '[update]   - %s\n' "${problems[@]}" >&2
    say "backup: ${BACKUP_PATH:-none}; database: $restored; fix by hand, then start the aecom-* services" >&2
    exit 1
  fi
  if ! svc start; then
    say "ROLLED BACK to ${FROM_COMMIT:0:7} but starting the services FAILED; start them by hand" >&2
    say "backup: ${BACKUP_PATH:-none}; database: $restored; doctor: $DOCTOR" >&2
    exit 1
  fi
  say "ROLLED BACK to ${FROM_COMMIT:0:7}; backup: ${BACKUP_PATH:-none}; database: $restored; doctor: $DOCTOR" >&2
  exit 1
}

# Services stay stopped for the whole window so no agent runs on half-updated code
# (Lemon Squeezy retries webhooks that arrive meanwhile).
STAGE="stopping services"
if ! svc stop; then
  say "could not stop services; aborting without changes" >&2
  svc start || true
  exit 1
fi

STAGE="backup"
if ! OUT=$(uv run aecom backup --label pre-update); then
  say "backup failed; aborting before any change" >&2
  svc start || true
  exit 1
fi
BACKUP_PATH=$(printf '%s\n' "$OUT" | tail -n 1)
say "backup: $BACKUP_PATH"

set -E
trap rollback ERR

STAGE="git pull"
git pull --ff-only
STAGE="uv sync"
uv sync
STAGE="aecom init"
uv run aecom init
# Doctor runs while the services are still stopped, so nothing can write orders,
# payments or licence keys between the check and a possible database restore.
STAGE="doctor"
if uv run aecom doctor >/dev/null 2>&1; then
  DOCTOR="ok"
else
  DOCTOR="FAILED"
  false
fi

# Past this point the services accept webhooks again: never restore the database
# after this (it would discard orders), only report.
trap - ERR
if ! svc start; then
  say "doctor passed but starting the services FAILED; start them by hand (database NOT rolled back)" >&2
  exit 1
fi
OK=0
for _ in $(seq 1 12); do
  if uv run aecom doctor >/dev/null 2>&1; then OK=1; break; fi
  sleep 5
done
if [ "$OK" -ne 1 ]; then
  say "services started but doctor FAILED within 60 s. NOT rolling back: the database now holds new orders. Run 'uv run aecom doctor', check the logs; backup: $BACKUP_PATH" >&2
  exit 1
fi
say "summary: ${FROM_COMMIT:0:7} -> $(git rev-parse --short HEAD); backup: $BACKUP_PATH; doctor: $DOCTOR"
