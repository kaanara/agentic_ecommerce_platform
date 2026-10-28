"""C3: backups, rotation, restore round-trip, verify drill (all on tmp databases)."""

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aecom import backup, cli, db
from aecom.bootstrap import seed_demo
from aecom.bus.events import drain
from aecom.config import get_settings
from aecom.store.adapter import get_store

SKU = "LIC-FOCUSFLOW-PRO-V1"
T0 = datetime(2026, 10, 28, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
def bk(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(get_settings(), "backup_dir", d)
    monkeypatch.setattr(get_settings(), "backup_target", None)
    return d


@pytest.fixture
def demo():
    seed_demo()
    get_store().create_order("c@example.com", SKU, "DE")
    drain()


def live() -> Path:
    return Path(get_settings().database_url.removeprefix("sqlite:///"))


def count(table: str, path: Path | None = None) -> int:
    con = sqlite3.connect(path or live())
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def run(*args) -> int:
    try:
        cli.main(["backup", *args])
    except SystemExit as e:
        return e.code or 0
    return 0


def test_create_copies_db_and_manifest(demo, bk):
    p = backup.create_backup(now=T0)
    assert p == bk / "20261028-030000"
    m = json.loads((p / "manifest.json").read_text())
    assert m["created_at"].startswith("2026-10-28T03:00:00")
    assert m["alembic_revision"]
    assert m["row_counts"]["orders"] == 1 and m["row_counts"]["customers"] >= 1
    assert set(m["row_counts"]) == {"orders", "customers", "ledger_entries", "events"}
    assert backup.check_hashes(p) == []
    assert backup._integrity(p / "aecom.db") == "ok"
    assert m["files"]["aecom.db"]["sha256"] == backup._sha256(p / "aecom.db")


def test_directory_contains_only_db_and_manifest(demo, bk, tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    con = sqlite3.connect(tmp_path / "aecom.checkpoints.db")
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    p = backup.create_backup(now=T0)
    names = sorted(f.name for f in p.iterdir())
    assert names == ["aecom.checkpoints.db", "aecom.db", "manifest.json"]
    assert not list(bk.glob(".tmp-*"))


def test_cli_prints_path_last_and_exit_codes(demo, bk, capsys):
    assert run("create", "--label", "pre update") == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert Path(last).parent == bk and last.endswith("-pre-update") and Path(last).is_dir()
    assert run("list") == 0
    assert "orders=1" in capsys.readouterr().out
    assert run("restore", last) == 1  # no --yes


def test_select_deletions_policy():
    now = datetime(2026, 10, 28, 12, 0)  # Wednesday
    daily = [(now - timedelta(days=i)).strftime("%Y%m%d-030000") for i in range(0, 60)]
    gone = set(backup.select_deletions(daily, now))
    kept = set(daily) - gone
    assert set(daily[:7]) <= kept
    assert all(datetime.strptime(n[:8], "%Y%m%d") >= datetime(2026, 9, 28) for n in kept)
    # one (the newest) per ISO week for the last 4 weeks, besides the newest 7
    assert {"20261018-030000", "20261011-030000"} <= kept
    assert "20261004-030000" in gone
    assert len(kept) < 14
    assert "20260901-030000" in gone


def test_labelled_retention_and_newest_never_deleted():
    now = datetime(2026, 10, 28, 12, 0)
    lab = [f"2026100{i}-040000-pre-update" for i in range(1, 9)]
    plain = ["20261027-030000"]
    assert set(backup.select_deletions(lab + plain, now)) == set(lab[:3])
    assert backup.select_deletions(["20200101-010101"], now) == []  # a lone old backup is the newest
    # newest overall is kept even when it is old and labelled


def test_rotate_only_touches_pattern_dirs(demo, bk, tmp_path):
    for i in range(12):
        backup.create_backup(now=T0 - timedelta(days=i))
    (bk / "notes").mkdir()
    (bk / "notes" / "keep.txt").write_text("x")
    (bk / "readme.txt").write_text("x")
    outside = tmp_path / "20200101-000000"
    outside.mkdir()
    deleted = backup.rotate(now=T0)
    assert deleted and "20261028-030000" not in deleted and (bk / "20261028-030000").exists()
    assert len(backup.list_backups()) == 12 - len(deleted)
    assert (bk / "notes" / "keep.txt").exists() and (bk / "readme.txt").exists()
    assert outside.exists()


def test_restore_round_trip(demo, bk, capsys):
    p = backup.create_backup(now=T0)
    orders = count("commerce_orders")
    get_store().create_order("d@example.com", SKU, "DE")
    drain()
    db.get_engine().dispose()
    assert count("commerce_orders") == orders + 1
    assert run("restore", str(p), "--yes") == 0
    assert count("commerce_orders") == orders
    before = list(live().parent.glob("aecom.db.before-restore-*"))
    assert len(before) == 1 and count("commerce_orders", before[0]) == orders + 1
    assert not live().with_name("aecom.db-wal").exists()
    assert "aecom init" in capsys.readouterr().out


def test_restore_refuses_without_yes(demo, bk):
    p = backup.create_backup(now=T0)
    before = count("commerce_orders")
    assert run("restore", str(p)) == 1
    assert count("commerce_orders") == before
    assert not list(live().parent.glob("*.before-restore-*"))


def test_restore_refuses_while_db_locked(demo, bk, capsys):
    p = backup.create_backup(now=T0)
    db.get_engine().dispose()
    holder = sqlite3.connect(live(), isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert run("restore", str(p), "--yes") == 1
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert "stop the services first" in capsys.readouterr().err
    assert not list(live().parent.glob("*.before-restore-*"))


def test_restore_refuses_damaged_backup(demo, bk):
    p = backup.create_backup(now=T0)
    with (p / "aecom.db").open("r+b") as f:
        f.seek(200)
        f.write(b"garbage!")
    assert run("restore", str(p), "--yes") == 1
    assert not list(live().parent.glob("*.before-restore-*"))


def test_verify_ok_and_does_not_touch_live_db(demo, bk, capsys):
    backup.create_backup(now=T0)
    url = get_settings().database_url
    assert run("verify") == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and rep["row_counts"]["orders"] == 1
    assert get_settings().database_url == url
    assert count("commerce_orders") == 1


def test_verify_detects_corrupted_copy(demo, bk, capsys):
    p = backup.create_backup(now=T0)
    with (p / "aecom.db").open("r+b") as f:
        f.seek(300)
        f.write(b"\xff" * 64)
    assert run("verify", str(p)) == 1
    assert "mismatch" in capsys.readouterr().out


def test_verify_runs_business_invariants(demo, bk, capsys):
    db.get_engine().dispose()
    con = sqlite3.connect(live())
    con.execute("DELETE FROM ledger_entries")
    con.commit()
    con.close()
    backup.create_backup(now=T0)
    assert run("verify") == 1
    assert "invariant" in capsys.readouterr().out


def test_target_failure_keeps_local_backup_and_exits_3(demo, bk, monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "backup_target", "pi@nas:/srv/aecom")

    def boom(*a, **k):
        raise subprocess.CalledProcessError(23, "rsync")

    monkeypatch.setattr(backup.subprocess, "run", boom)
    recorded = []
    monkeypatch.setattr("aecom.observability.record_error", lambda *a, **k: recorded.append(a))
    assert run("create") == 3
    out = capsys.readouterr().out.strip().splitlines()
    assert Path(out[-1]).is_dir() and backup.check_hashes(Path(out[-1])) == []
    assert recorded and recorded[0][0] == "backup.target"


def test_local_target_mirrors(demo, bk, tmp_path, monkeypatch):
    usb = tmp_path / "usb"
    monkeypatch.setattr(get_settings(), "backup_target", str(usb))
    assert run("create") == 0
    assert [p.name for p in usb.iterdir()] == [p.name for p in backup.list_backups()]
    assert backup.check_hashes(next(usb.iterdir())) == []


def test_remote_target_detection():
    assert backup.is_remote("pi@nas:/srv/aecom") and backup.is_remote("nas:backups")
    assert not backup.is_remote("/mnt/usb") and not backup.is_remote("./rel") and not backup.is_remote("~/x")


def test_postgres_url_gives_clear_error(bk, monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "database_url", "postgresql+psycopg://u:p@h/db")
    with pytest.raises(backup.BackupError, match="pg_dump"):
        backup.create_backup()
    assert run("create") == 1
    assert "pg_dump" in capsys.readouterr().err
