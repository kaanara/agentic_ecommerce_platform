# Backups

The company's state lives in two SQLite files. `aecom backup` copies them safely while the services keep running. UI-driven backups are available from the dashboard [Settings](Settings) > Backups and updates tab.

## What is backed up

- `data/aecom.db` - orders, customers, ledger, events, approvals, audit log.
- `data/aecom.checkpoints.db` - the LangGraph checkpoints (if present).
- `manifest.json` - UTC time, git sha, migration revision, size and SHA-256 per file, row counts of orders,
  customers, ledger entries and events, verification status, and off-device copy outcome.

The copies use SQLite's online backup API (never a plain file copy, which can be torn while the database is in
use), and every copy must pass `PRAGMA integrity_check` or the backup fails. A backup directory contains only
`*.db` files and `manifest.json`. **`.env` is never included**: keep it in the owner's password manager.

PostgreSQL is not handled by this command (it raises an error pointing to `pg_dump`).

## Dashboard interface

**[Settings](Settings) > Backups and updates**:
- **Automatic backups** form at the top: a switch to turn scheduled backups on or off, how often (every few hours, every day or every week), the time of day and weekday, and whether to verify each backup after it is made. No cron string needed; it saves to the `backup_enabled`, `backup_schedule` and `backup_verify_after` settings and the scheduler picks the change up within about 30 seconds.
- List of recent backups with their timestamp, size, schema revision, git sha, encryption (✅/—), verification status (✅/—), and off-device copy status (✅/❌/—).
- Next scheduled run (computed from `backup_schedule` setting).
- Free disk space where backups are stored.
- **Back up now**: creates a backup on demand (published to the event bus; the scheduler runs it and writes the result to the jobs table).
- **Verify newest**: runs an integrity check and business invariant validation on the newest backup.
- **Restore** and **Download**: require the dashboard password again (customer data in backups). Restore prompts for the backup name in full and refuses to restore while the database is in use.

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

The dashboard Settings can enable automatic backups with a schedule (`BACKUP_ENABLED`, `BACKUP_SCHEDULE` — same interval/cron grammar as other scheduler jobs). Or run `aecom backup` from a cron job or systemd timer. Use `--label pre-update` right before every update. `aecom doctor` warns when the newest backup is older than 26 h and
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

A backup on the same SD card is not a backup. Set `BACKUP_TARGET` in `.env` or [Settings](Settings) to one of:

- a local path, e.g. a mounted USB drive: `BACKUP_TARGET=/mnt/usb/aecom-backups`
- a second machine over ssh: `BACKUP_TARGET=backup@nas.local:/srv/aecom-backups` (uses
  `rsync -a --delete-after` with key-based ssh, so set up the key first)
- a cloud bucket via rclone, for example after `rclone config` add an `ExecStartPost=` line to the timer service:
  `rclone sync /home/pi/aecom/data/backups remote:aecom-backups`

If the copy fails the local backup is kept, the error is recorded (see `aecom errors`) and `create` exits 3. A stale backup alert (P1 if over 3 days, P2 if over 26 h) fires if an off-device target is configured and the last copy failed.

## Encryption at rest

Set the secret `BACKUP_PASSPHRASE` (Settings > Backups and updates, or `.env`; at least 12 characters) and every **new** backup stores its database files encrypted as `<name>.enc` (AES-256-GCM, key derived from the passphrase with scrypt); the plain copy is deleted, and the manifest says `"encrypted": true`. Verify and restore decrypt automatically with the same passphrase. A wrong or missing passphrase is refused with a clear message, and restore unpacks and checks everything before it touches the live database.

- **The passphrase is not in any backup**, so keep it in a password manager. A lost passphrase means the encrypted backups cannot be restored.
- Backups made before you set it stay plain; changing the passphrase later does not re-encrypt old backups.
- If the passphrase is set but shorter than 12 characters, `aecom backup` fails instead of writing a plain backup.
- The whole file is encrypted in memory, which is fine for the database sizes this project targets.
- This protects copies at rest, including the off-device copy. It does not replace a trusted target.

## Privacy

Backups contain customer personal data (names, emails, orders). Only use a target you trust and that is
encrypted (an encrypted USB volume, an ssh host you control, a bucket with server-side encryption). Do not put
backups into shared folders or email. Delete backups of customers who requested erasure only through the normal
retention rotation; they age out within about 5 weeks.
