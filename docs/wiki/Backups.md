# Backups

The company's state lives in two SQLite files. `aecom backup` copies them safely while the services keep running.

## What is backed up

- `data/aecom.db` - orders, customers, ledger, events, approvals, audit log.
- `data/aecom.checkpoints.db` - the LangGraph checkpoints (if present).
- `manifest.json` - UTC time, git sha, migration revision, size and SHA-256 per file, row counts of orders,
  customers, ledger entries and events.

The copies use SQLite's online backup API (never a plain file copy, which can be torn while the database is in
use), and every copy must pass `PRAGMA integrity_check` or the backup fails. A backup directory contains only
`*.db` files and `manifest.json`. **`.env` is never included**: keep it in the owner's password manager.

PostgreSQL is not handled by this command (it raises an error pointing to `pg_dump`).

## Commands

```
uv run aecom backup [create] [--label NAME] [--dest DIR]   # last stdout line = the backup directory
uv run aecom backup list
uv run aecom backup verify [PATH]                          # newest by default
uv run aecom backup restore PATH --yes
```

Exit codes of `create`: 0 ok, 3 when the optional off-device copy failed (the local backup is kept), 1 any other
error. Backups land in `data/backups/YYYYMMDD-HHMMSS[-label]/` (setting `BACKUP_DIR`).

## Schedule

Run `aecom backup` daily from the systemd timer described in the Raspberry Pi guide, and with
`--label pre-update` right before every update. `aecom doctor` warns when the newest backup is older than 26 h and
fails after 72 h, or when its hashes no longer match the manifest.

## Retention

After each backup: the newest 7 unlabelled (daily) backups plus the newest one of each of the last 4 ISO weeks.
Labelled backups (e.g. `pre-update`) are kept separately, newest 5 only. The newest backup is never deleted, and
only directories named like a backup inside `BACKUP_DIR` are ever removed.

## Restore steps

1. Stop the services (scheduler, dashboard, API). Restore refuses while the database is in use.
2. `uv run aecom backup verify PATH` (optional but cheap).
3. `uv run aecom backup restore PATH --yes`. The replaced database is kept as
   `data/aecom.db.before-restore-<timestamp>`; stale `-wal`/`-shm` files are removed.
4. `uv run aecom init` (applies newer migrations), then `uv run aecom doctor`. Start the services.

## Monthly drill

Once a month run `uv run aecom backup verify`. It restores the newest backup into a temporary directory, checks
integrity and hashes, runs the business invariants (balanced ledger, paid orders delivered, ...) against the copy
and prints the row counts. It never touches the live database. Exit 1 means the backup is not trustworthy: take a
fresh one and look at the reported problems.

## Off-device copy

A backup on the same SD card is not a backup. Set `BACKUP_TARGET` in `.env` to one of:

- a local path, e.g. a mounted USB drive: `BACKUP_TARGET=/mnt/usb/aecom-backups`
- a second machine over ssh: `BACKUP_TARGET=backup@nas.local:/srv/aecom-backups` (uses
  `rsync -a --delete-after` with key-based ssh, so set up the key first)
- a cloud bucket via rclone, for example after `rclone config` add an `ExecStartPost=` line to the timer service:
  `rclone sync /home/pi/aecom/data/backups remote:aecom-backups`

If the copy fails the local backup is kept, the error is recorded (see `aecom errors`) and `create` exits 3.

## Privacy

Backups contain customer personal data (names, emails, orders). Only use a target you trust and that is
encrypted (an encrypted USB volume, an ssh host you control, a bucket with server-side encryption). Do not put
backups into shared folders or email. Delete backups of customers who requested erasure only through the normal
retention rotation; they age out within about 5 weeks.
