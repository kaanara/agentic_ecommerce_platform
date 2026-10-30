"""Backups, restore and the restore drill (C3). SQLite only; Postgres is backed up with ``pg_dump``.

A backup is a directory ``<BACKUP_DIR>/YYYYMMDD-HHMMSS[-label]/`` holding online-API copies of the main database
and the LangGraph checkpoints database (if present) plus ``manifest.json``. Nothing else is ever included:
no ``.env``, no keys, no files outside the database files.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aecom.config import ROOT, get_settings

NAME_RE = re.compile(r"^(\d{8})-(\d{6})(?:-([A-Za-z0-9][A-Za-z0-9_.-]*))?$")
KEEP_DAILY = 7
KEEP_WEEKS = 4
KEEP_LABELLED = 5
KEY_TABLES = {"orders": "commerce_orders", "customers": "commerce_customers",
              "ledger_entries": "ledger_entries", "events": "ops_events"}
RSYNC_TIMEOUT_S = 900
EXIT_OK, EXIT_ERROR, EXIT_TARGET_FAILED = 0, 1, 3


class BackupError(Exception):
    pass


class TargetError(BackupError):
    """The off-device copy failed; the local backup is kept."""


# --- paths ---------------------------------------------------------------------------------------------------

def backup_dir(dest: str | Path | None = None) -> Path:
    return Path(dest) if dest else Path(get_settings().backup_dir)


def live_db_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise BackupError("`aecom backup` is SQLite only; for PostgreSQL use pg_dump "
                          "(e.g. pg_dump -Fc \"$DATABASE_URL\" > aecom.dump) and restore with pg_restore")
    return Path(url.removeprefix("sqlite:///"))


def checkpoints_path(main: Path) -> Path:
    return main.with_name(main.stem + ".checkpoints.db")


def _is_checkpoints(name: str) -> bool:
    return name.endswith(".checkpoints.db")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- sqlite helpers ------------------------------------------------------------------------------------------

def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def _integrity(path: Path) -> str:
    con = _ro(path)
    try:
        return str(con.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as e:
        return f"unreadable: {e}"
    finally:
        con.close()


def _count_rows(path: Path) -> dict[str, int | None]:
    con = _ro(path)
    try:
        out: dict[str, int | None] = {}
        for key, table in KEY_TABLES.items():
            try:
                out[key] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[key] = None
        return out
    finally:
        con.close()


def _alembic_revision(path: Path) -> str | None:
    con = _ro(path)
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _online_copy(src: Path, dst: Path) -> None:
    """Consistent copy of a live database with sqlite3's online backup API (never a file copy)."""
    s = sqlite3.connect(src, timeout=30)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm next to it
    finally:
        d.close()
        s.close()


def _git_sha() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                           timeout=5)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# --- create --------------------------------------------------------------------------------------------------

def _clean_label(label: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", (label or "").strip()).strip("-.")


def create_backup(label: str | None = None, dest: str | Path | None = None, now: datetime | None = None) -> Path:
    main = live_db_path()
    if not main.exists():
        raise BackupError(f"database {main} does not exist; nothing to back up")
    now = now or _utcnow()
    label = _clean_label(label)
    name = now.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
    root = backup_dir(dest)
    target = root / name
    if target.exists():
        raise BackupError(f"backup {target} already exists (wait a second and retry)")
    root.mkdir(parents=True, exist_ok=True)
    work = root / f".tmp-{name}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        sources = [main] + ([checkpoints_path(main)] if checkpoints_path(main).exists() else [])
        files: dict[str, Any] = {}
        for src in sources:
            copy = work / src.name
            _online_copy(src, copy)
            res = _integrity(copy)
            if res != "ok":
                raise BackupError(f"integrity_check of the copy of {src.name} failed: {res}")
            files[src.name] = {"bytes": copy.stat().st_size, "sha256": _sha256(copy)}
        manifest = {"created_at": now.astimezone(UTC).isoformat(), "git_sha": _git_sha(),
                    "alembic_revision": _alembic_revision(work / main.name), "files": files,
                    "row_counts": _count_rows(work / main.name)}
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        work.rename(target)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return target


# --- listing / manifest --------------------------------------------------------------------------------------

def list_backups(dest: str | Path | None = None) -> list[Path]:
    """Backup directories matching the naming pattern, newest first."""
    root = backup_dir(dest)
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and NAME_RE.match(p.name)),
                  key=lambda p: p.name[:15], reverse=True)


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        return json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BackupError(f"{path}: manifest.json unreadable ({e})") from e


def created_at(path: Path) -> datetime:
    try:
        return datetime.fromisoformat(read_manifest(path)["created_at"])
    except (BackupError, KeyError, ValueError):
        return datetime.strptime(Path(path).name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)


def update_manifest(path: Path, **fields: Any) -> None:
    """Patch extra fields (``verified``, ``verified_at``, ``copied_off_device``) into an existing manifest.json,
    used by the job handlers below so the Backups tab and the stale-backup monitor (§5c) can read per-backup status
    without a separate table. Never raises: a manifest that cannot be read/written is left alone."""
    path = Path(path)
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    manifest.update(fields)
    try:
        (path / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def check_hashes(path: Path) -> list[str]:
    """Problems found comparing the files to the manifest (empty list = intact)."""
    path = Path(path)
    try:
        files = read_manifest(path)["files"]
    except (BackupError, KeyError) as e:
        return [str(e) or "manifest has no files"]
    if not files:
        return ["manifest lists no files"]
    problems = []
    for name, meta in files.items():
        f = path / name
        if not f.is_file():
            problems.append(f"{name}: missing")
        elif f.stat().st_size != meta.get("bytes") or _sha256(f) != meta.get("sha256"):
            problems.append(f"{name}: hash/size mismatch")
    return problems


# --- rotation ------------------------------------------------------------------------------------------------

def _retention() -> tuple[int, int, int]:
    """(keep_daily, keep_weeks, keep_labelled), overridable from the settings registry (Settings > Backups) once
    ``backup_keep_daily``/``backup_keep_weeks``/``backup_keep_labelled`` land there; the built-in defaults until then."""
    s = get_settings()
    return (getattr(s, "backup_keep_daily", KEEP_DAILY), getattr(s, "backup_keep_weeks", KEEP_WEEKS),
            getattr(s, "backup_keep_labelled", KEEP_LABELLED))


def select_deletions(names: list[str], now: datetime, keep_daily: int | None = None, keep_weeks: int | None = None,
                     keep_labelled: int | None = None) -> list[str]:
    """Pure retention policy over backup directory names. Keeps the newest ``keep_daily`` unlabelled, the newest of
    each of the last ``keep_weeks`` ISO weeks, the newest ``keep_labelled`` labelled, and always the newest backup
    of all. Defaults come from the settings registry (or the built-in constants before those keys exist)."""
    default_daily, default_weeks, default_labelled = _retention()
    keep_daily = default_daily if keep_daily is None else keep_daily
    keep_weeks = default_weeks if keep_weeks is None else keep_weeks
    keep_labelled = default_labelled if keep_labelled is None else keep_labelled
    parsed = []
    for n in names:
        m = NAME_RE.match(n)
        if m:
            parsed.append((datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"), n, bool(m.group(3))))
    parsed.sort(reverse=True)
    keep: set[str] = set()
    if parsed:
        keep.add(parsed[0][1])
    plain = [p for p in parsed if not p[2]]
    keep.update(n for _, n, _lab in plain[:keep_daily])
    this_monday = (now - timedelta(days=now.weekday())).date()
    seen: set[tuple[int, int]] = set()
    for ts, n, _lab in plain:  # newest first, so the first of a week is that week's newest
        week = tuple(ts.isocalendar()[:2])
        if week in seen:
            continue
        seen.add(week)  # type: ignore[arg-type]
        if 0 <= (this_monday - (ts - timedelta(days=ts.weekday())).date()).days // 7 < keep_weeks:
            keep.add(n)
    keep.update(n for _, n, _lab in [p for p in parsed if p[2]][:keep_labelled])
    return [n for _, n, _lab in parsed if n not in keep]


def rotate(dest: str | Path | None = None, now: datetime | None = None) -> list[str]:
    root = backup_dir(dest).resolve()
    names = [p.name for p in list_backups(root)]
    deleted = []
    for n in select_deletions(names, now or _utcnow()):
        p = root / n
        if p.is_symlink() or p.resolve().parent != root or not NAME_RE.match(p.name):
            continue  # deletion only inside BACKUP_DIR and only for directories matching the pattern
        shutil.rmtree(p)
        deleted.append(n)
    return deleted


# --- off-device copy -----------------------------------------------------------------------------------------

REMOTE_RE = re.compile(r"^(?:[^/@\s]+@)?[^/:\s.~][^/:\s]*:.+$")


def is_remote(target: str) -> bool:
    return not target.startswith(("/", ".", "~")) and bool(REMOTE_RE.match(target))


def copy_to_target(target: str, dest: str | Path | None = None) -> None:
    """Mirror BACKUP_DIR to the target: rsync over ssh for user@host:/path, plain copy for a local path."""
    root = backup_dir(dest)
    if is_remote(target):
        cmd = ["rsync", "-a", "--delete-after", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=20",
               "--include=/????????-??????*/", "--include=/????????-??????*/*", "--exclude=*",
               f"{root}/", target.rstrip("/") + "/"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=RSYNC_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            raise TargetError(f"rsync to {target} failed: {e}") from e
        if r.returncode != 0:
            raise TargetError(f"rsync to {target} failed ({r.returncode}): {r.stderr.strip()[:300]}")
        return
    out = Path(target).expanduser()
    try:
        out.mkdir(parents=True, exist_ok=True)
        wanted = {p.name for p in list_backups(root)}
        for p in list_backups(root):
            if not (out / p.name).exists():
                tmp = out / f".tmp-{p.name}"
                shutil.rmtree(tmp, ignore_errors=True)
                shutil.copytree(p, tmp)
                tmp.rename(out / p.name)
        for old in list_backups(out):
            if old.name not in wanted:
                shutil.rmtree(old)
    except OSError as e:
        raise TargetError(f"copy to {out} failed: {e}") from e


# --- verify --------------------------------------------------------------------------------------------------

def verify_backup(path: Path) -> dict[str, Any]:
    """Restore into a temp dir, integrity-check, compare hashes, run the business invariants on the copy.
    Never touches the configured database."""
    from aecom import db
    from aecom.chaos import invariants

    path = Path(path)
    report: dict[str, Any] = {"path": str(path), "ok": False, "problems": []}
    problems: list[str] = report["problems"]
    problems += check_hashes(path)
    if problems:
        return report
    manifest = read_manifest(path)
    with tempfile.TemporaryDirectory(prefix="aecom-verify-") as tmp:
        tmpd = Path(tmp)
        main_name = None
        for name in manifest["files"]:
            shutil.copy2(path / name, tmpd / name)
            if not _is_checkpoints(name):
                main_name = name
            res = _integrity(tmpd / name)
            if res != "ok":
                problems.append(f"{name}: integrity_check: {res}")
        if main_name is None:
            problems.append("backup has no main database")
        if problems:
            return report
        report["row_counts"] = _count_rows(tmpd / main_name)
        report["alembic_revision"] = _alembic_revision(tmpd / main_name)
        settings = get_settings()
        saved_url = settings.database_url
        violations: list[Any] = []
        try:
            settings.database_url = f"sqlite:///{tmpd / main_name}"
            db.reset_engine()
            violations = invariants.check_all()
        except Exception as e:  # noqa: BLE001 - a restored copy that cannot be queried is a failed drill
            problems.append(f"invariants could not run: {type(e).__name__}: {e}")
        finally:
            if db._engine is not None:
                db._engine.dispose()
            settings.database_url = saved_url
            db.reset_engine()
        for v in violations:
            problems.append(f"invariant {v['invariant']}: {v['detail']}")
    report["ok"] = not problems
    return report


# --- restore -------------------------------------------------------------------------------------------------

def _assert_not_in_use(main: Path) -> None:
    if not main.exists():
        return
    con = sqlite3.connect(main, timeout=1, isolation_level=None)
    try:
        con.execute("BEGIN EXCLUSIVE")
        con.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        raise BackupError("the live database is in use; stop the services first "
                          "(scheduler, dashboard, API), then retry") from e
    finally:
        con.close()


def restore_backup(path: Path, now: datetime | None = None) -> dict[str, Any]:
    path = Path(path)
    main = live_db_path()
    problems = check_hashes(path)
    if problems:
        raise BackupError("backup is damaged, not restoring: " + "; ".join(problems))
    files = read_manifest(path)["files"]
    _assert_not_in_use(main)
    stamp = (now or _utcnow()).strftime("%Y%m%d-%H%M%S")
    saved, restored = [], []
    main.parent.mkdir(parents=True, exist_ok=True)
    for name in files:
        live = checkpoints_path(main) if _is_checkpoints(name) else main
        if live.exists():
            keep = live.with_name(f"{live.name}.before-restore-{stamp}")
            _online_copy(live, keep)
            saved.append(str(keep))
        tmp = live.with_name(live.name + ".restoring")
        shutil.copy2(path / name, tmp)
        tmp.replace(live)
        for suffix in ("-wal", "-shm"):
            live.with_name(live.name + suffix).unlink(missing_ok=True)
        restored.append(str(live))
    return {"restored": restored, "previous_saved_as": saved,
            "next": "run `uv run aecom init` to apply any newer migrations, then `uv run aecom doctor`"}


# --- CLI glue ------------------------------------------------------------------------------------------------

def run_cli(action: str, path: str | None = None, *, label: str | None = None, dest: str | None = None,
            yes: bool = False) -> int:
    """Returns the process exit code. ``create`` prints the backup directory as the LAST stdout line."""

    def err(msg: str) -> int:
        print(f"backup: {msg}", file=sys.stderr)
        return EXIT_ERROR

    try:
        if action == "create":
            target = create_backup(label, dest)
            code = EXIT_OK
            deleted = rotate(dest)
            if deleted:
                print(f"rotated away {len(deleted)} old backup(s)", file=sys.stderr)
            bt = get_settings().backup_target
            if bt:
                try:
                    copy_to_target(bt, dest)
                    update_manifest(target, copied_off_device=True)
                except TargetError as e:
                    from aecom.observability import record_error

                    update_manifest(target, copied_off_device=False)
                    record_error("backup.target", e, context={"target": bt})
                    print(f"backup: off-device copy failed, local backup kept: {e}", file=sys.stderr)
                    code = EXIT_TARGET_FAILED
            print(target)
            return code
        if action == "list":
            for p in list_backups(dest):
                try:
                    m = read_manifest(p)
                    size = sum(f["bytes"] for f in m["files"].values())
                    print(f"{p.name}  {size / 1024:.0f} KiB  orders={m.get('row_counts', {}).get('orders')}  "
                          f"rev={m.get('alembic_revision')}  git={m.get('git_sha')}")
                except (BackupError, KeyError):
                    print(f"{p.name}  (manifest unreadable)")
            return EXIT_OK
        if action == "verify":
            target = Path(path) if path else next(iter(list_backups(dest)), None)
            if target is None:
                return err("no backups to verify")
            rep = verify_backup(target)
            if rep["ok"]:
                update_manifest(target, verified=True, verified_at=_utcnow().isoformat())
            print(json.dumps(rep, indent=2, default=str))
            return EXIT_OK if rep["ok"] else EXIT_ERROR
        if action == "restore":
            if not path:
                return err("usage: aecom backup restore PATH --yes")
            if not yes:
                return err("restore replaces the live database; re-run with --yes (stop the services first)")
            print(json.dumps(restore_backup(Path(path)), indent=2))
            return EXIT_OK
        return err(f"unknown action {action!r}")
    except BackupError as e:
        return err(str(e))


# --- UI jobs and events (§5c: backup now / verify now, started and scheduled from the dashboard) --------------
#
# The dashboard only ever *publishes* ``backup.requested``/``backup.verify_requested`` on the durable event bus
# (never runs the backup itself): a subscriber here turns that into an ``ops_jobs`` row, and the scheduler's
# ``jobs.run_pending()`` tick (see the ``scheduler.py`` patch in the wave-2 report) runs the handler registered
# below, out of the dashboard process. Restore is deliberately NOT wired this way (see ``restore_apply_from_settings``):
# it needs the scheduler itself stopped, so it can never run from inside ``run_pending``.

def run_backup_job(params: dict[str, Any]) -> dict[str, Any]:
    """``jobs`` handler for kind ``backup``: create, rotate, optionally copy off-device and verify. Problems with
    the off-device copy or the post-backup verify are recorded in the result (so the Backups tab and the
    stale-backup monitor can see them) rather than raised, mirroring how ``doctor`` jobs report findings without
    failing the job itself. Only a failure to create the local backup at all raises (nothing usable happened)."""
    settings = get_settings()
    target = create_backup(label=params.get("label"))
    result: dict[str, Any] = {"backup": str(target)}
    result["rotated"] = rotate()
    bt = settings.backup_target
    if bt:
        try:
            copy_to_target(bt)
            update_manifest(target, copied_off_device=True)
            result["copied_off_device"] = True
        except TargetError as e:
            from aecom.observability import record_error

            update_manifest(target, copied_off_device=False)
            record_error("backup.target", e, context={"target": bt})
            result["copied_off_device"] = False
            result["target_error"] = str(e)
    if settings.backup_verify_after:
        report = verify_backup(target)
        if report["ok"]:
            update_manifest(target, verified=True, verified_at=_utcnow().isoformat())
        result["verified"] = report["ok"]
        result["verify_problems"] = report["problems"]
    return result


def run_verify_job(params: dict[str, Any]) -> dict[str, Any]:
    """``jobs`` handler for kind ``verify``: verify a named backup, or the newest one. Raises only when there is
    nothing to verify; a failed verify is a normal (not-ok) result, not a job error."""
    name = params.get("backup")
    target = backup_dir() / name if name else next(iter(list_backups()), None)
    if target is None:
        raise BackupError("no backups to verify")
    report = verify_backup(Path(target))
    if report["ok"]:
        update_manifest(Path(target), verified=True, verified_at=_utcnow().isoformat())
    return report


def _on_backup_requested(payload: dict[str, Any]) -> None:
    from aecom import jobs

    jobs.enqueue("backup", str(payload.get("requested_by") or ""), {"label": payload.get("label")})


def _on_backup_verify_requested(payload: dict[str, Any]) -> None:
    from aecom import jobs

    jobs.enqueue("verify", str(payload.get("requested_by") or ""), {"backup": payload.get("backup")})


def _register() -> None:
    """Idempotent: importing this module twice (tests, reload) must not double-subscribe or re-raise."""
    from aecom import jobs
    from aecom.bus.events import subscribe

    jobs.register("backup", run_backup_job)
    jobs.register("verify", run_verify_job)
    subscribe("backup.requested")(_on_backup_requested)
    subscribe("backup.verify_requested")(_on_backup_verify_requested)


_register()


# --- restore from the UI (§5c): ops_settings "restore_requested" + the aecom-restore.service one-shot unit ----
#
# Restore is critical and needs the scheduler/API/dashboard stopped first, so it is never run through the normal
# ops_jobs queue. The dashboard (step-up + typed backup name) only sets the ``restore_requested`` ops_setting and
# either starts ``aecom-restore.service`` (with the opt-in sudo rule) or shows the operator the exact commands.
# The unit's ExecStart is ``uv run python -m aecom.backup``, which calls this function.

def restore_apply_from_settings(now: datetime | None = None) -> dict[str, Any]:
    """Restore the backup named by the ``restore_requested`` ops_setting. Verifies it first, takes a pre-restore
    safety backup, restores, runs ``aecom doctor``, writes the result to ``ops_jobs`` and alerts, then clears the
    request. Never raises: every failure is recorded in the returned/stored result (``"error"`` key) instead, so the
    one-shot unit can always start the services back up."""
    from aecom import jobs
    from aecom.company_settings import get_setting, set_setting

    t = now or _utcnow()
    name = (get_setting("restore_requested") or "").strip()
    result: dict[str, Any] = {"requested": name}
    status = "error"
    if not name:
        result["error"] = "no restore_requested setting; nothing to do"
    else:
        try:
            path = backup_dir() / name
            if not path.is_dir():
                raise BackupError(f"backup {name!r} not found in {backup_dir()}")
            report = verify_backup(path)
            result["verify"] = report
            if not report["ok"]:
                raise BackupError("backup failed verification, not restoring: " + "; ".join(report["problems"]))
            pre = create_backup(label="pre-restore", now=t)
            result["pre_restore_backup"] = str(pre)
            result["restore"] = restore_backup(path, now=t)
            from aecom import doctor

            result["doctor"] = doctor.run_checks()
            status = "ok"
        except Exception as exc:  # noqa: BLE001 - must always record and alert, never crash the unit
            result["error"] = f"{type(exc).__name__}: {exc}"
    jobs.record("restore", "system", result, status=status)
    set_setting("restore_requested", "")
    try:
        from aecom import alerts

        alerts.route({"monitor": "restore", "severity": "P1" if status == "error" else "P2", "subject": name,
                     "detail": f"restore of {name!r}: {status}" + (f" - {result['error']}" if "error" in result else "")})
    except Exception:  # noqa: BLE001 - the restore result is already recorded; alerting is best-effort
        pass
    return result


if __name__ == "__main__":  # pragma: no cover - exercised by deploy/pi/aecom-restore.service, not by pytest
    import sys as _sys

    from aecom.bootstrap import init as _init

    _init()
    _res = restore_apply_from_settings()
    _sys.exit(0 if "error" not in _res else 1)
