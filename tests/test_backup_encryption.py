"""R18: encrypted backups (AES-256-GCM, passphrase in .env) and the friendly backup schedule form."""

import sqlite3
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from aecom import backup, backup_crypto
from aecom import settings_registry as reg
from aecom.bootstrap import seed_demo
from aecom.config import get_settings

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))
PASS = "correct horse battery staple"


@pytest.fixture
def bk(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "backup_dir", tmp_path / "backups")
    monkeypatch.setattr(get_settings(), "backup_target", None)
    monkeypatch.delenv("BACKUP_PASSPHRASE", raising=False)
    seed_demo()
    return tmp_path / "backups"


def live() -> Path:
    return Path(get_settings().database_url.removeprefix("sqlite:///"))


def orders(path: Path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM commerce_orders").fetchone()[0]
    finally:
        con.close()


# --------------------------------------------------------------------------- the crypto layer
def test_round_trip_and_the_file_is_not_readable_as_sqlite(tmp_path):
    src = tmp_path / "a.db"
    src.write_bytes(b"SQLite format 3\x00" + b"secret customer data" * 50)
    enc, out = tmp_path / "a.db.enc", tmp_path / "out.db"
    backup_crypto.encrypt_file(src, enc, PASS)
    assert b"secret customer data" not in enc.read_bytes() and enc.read_bytes().startswith(backup_crypto.MAGIC)
    backup_crypto.decrypt_file(enc, out, PASS)
    assert out.read_bytes() == src.read_bytes()


def test_wrong_or_missing_passphrase_and_tampering_are_refused(tmp_path):
    src, enc = tmp_path / "a.db", tmp_path / "a.db.enc"
    src.write_bytes(b"x" * 100)
    backup_crypto.encrypt_file(src, enc, PASS)
    with pytest.raises(backup_crypto.CryptoError, match="wrong BACKUP_PASSPHRASE"):
        backup_crypto.decrypt_file(enc, tmp_path / "o", "another long passphrase")
    with pytest.raises(backup_crypto.CryptoError, match="set BACKUP_PASSPHRASE"):
        backup_crypto.decrypt_file(enc, tmp_path / "o", None)
    raw = bytearray(enc.read_bytes())
    raw[-1] ^= 1
    enc.write_bytes(bytes(raw))
    with pytest.raises(backup_crypto.CryptoError):
        backup_crypto.decrypt_file(enc, tmp_path / "o", PASS)


def test_short_passphrases_are_rejected():
    with pytest.raises(backup_crypto.CryptoError, match="at least 12"):
        backup_crypto.check_passphrase("short")


# --------------------------------------------------------------------------- backups
def test_without_a_passphrase_backups_stay_plain(bk):
    path = backup.create_backup()
    m = backup.read_manifest(path)
    assert m["encrypted"] is False and (path / live().name).is_file()


def test_encrypted_backup_has_no_plain_database_and_verifies(bk, monkeypatch):
    monkeypatch.setenv("BACKUP_PASSPHRASE", PASS)
    path = backup.create_backup()
    m = backup.read_manifest(path)
    assert m["encrypted"] is True and m["row_counts"]["orders"] is not None and m["alembic_revision"]
    assert not (path / live().name).exists()
    assert all(n.endswith(".enc") for n in m["files"]) and all(f["encrypted"] for f in m["files"].values())
    assert backup.check_hashes(path) == []
    assert backup.verify_backup(path)["ok"] is True


def test_verify_without_the_passphrase_fails_clearly(bk, monkeypatch):
    monkeypatch.setenv("BACKUP_PASSPHRASE", PASS)
    path = backup.create_backup()
    monkeypatch.delenv("BACKUP_PASSPHRASE")
    rep = backup.verify_backup(path)
    assert rep["ok"] is False and any("BACKUP_PASSPHRASE" in p for p in rep["problems"])


def test_restore_decrypts_and_a_wrong_passphrase_changes_nothing(bk, monkeypatch):
    monkeypatch.setenv("BACKUP_PASSPHRASE", PASS)
    path = backup.create_backup()
    before = orders(live())
    monkeypatch.setenv("BACKUP_PASSPHRASE", "a different long passphrase")
    with pytest.raises(backup.BackupError, match="wrong BACKUP_PASSPHRASE"):
        backup.restore_backup(path)
    assert orders(live()) == before and not list(live().parent.glob("*.restoring"))
    monkeypatch.setenv("BACKUP_PASSPHRASE", PASS)
    out = backup.restore_backup(path)
    assert out["restored"] and orders(live()) == before


def test_too_short_passphrase_blocks_the_backup_instead_of_writing_a_plain_one(bk, monkeypatch):
    monkeypatch.setenv("BACKUP_PASSPHRASE", "short")
    with pytest.raises(backup.BackupError, match="at least 12"):
        backup.create_backup()
    assert backup.list_backups() == []


def test_passphrase_is_a_registry_secret_so_the_settings_page_can_set_it():
    f = reg.FIELDS["backup_passphrase"]
    assert f.kind == "secret" and f.section == "Backups and updates"


# --------------------------------------------------------------------------- schedule form helpers
@pytest.mark.parametrize(("args", "spec"), [
    (("Every day", 6, 3, 15, "Monday"), "cron:15 3 * * *"),
    (("Every few hours", 6, 3, 15, "Monday"), "6h"),
    (("Every week", 6, 22, 5, "Sunday"), "cron:5 22 * * 7"),
])
def test_schedule_from_form_builds_valid_specs(args, spec):
    from _settings_backups import schedule_from_form

    from aecom.schedule_spec import is_valid

    assert schedule_from_form(*args) == spec and is_valid(spec)


@pytest.mark.parametrize("spec", ["cron:15 3 * * *", "12h", "cron:5 22 * * 7", "cron:30 6 * * 3"])
def test_form_round_trips_the_specs_it_creates(spec):
    from _settings_backups import form_from_schedule, schedule_from_form

    f = form_from_schedule(spec)
    assert not f["custom"]
    assert schedule_from_form(f["frequency"], f["hours"], f["hour"], f["minute"], f["weekday"]) == spec


def test_unusual_schedules_are_flagged_custom():
    from _settings_backups import form_from_schedule

    assert form_from_schedule("cron:0 7 1 * *")["custom"] is True


# --------------------------------------------------------------------------- the form on the page
def _tab():
    import streamlit as st

    from _settings_backups import render_backups_tab

    st.session_state.setdefault("x", 1)
    render_backups_tab("Ada")


def test_saving_the_form_updates_the_settings(bk, monkeypatch):
    monkeypatch.setattr(get_settings(), "backup_enabled", True)
    monkeypatch.setattr(get_settings(), "backup_schedule", "cron:15 3 * * *")
    at = AppTest.from_function(_tab, default_timeout=30).run()
    assert not at.exception
    at.toggle(key="_bk_enabled").set_value(False)
    at.radio(key="_bk_frequency").set_value("Every few hours")
    at.number_input(key="_bk_hours").set_value(4)
    at.button[0].click().run()
    assert not at.exception and not at.error
    assert get_settings().backup_schedule == "4h" and get_settings().backup_enabled is False
    assert reg.value("backup_schedule") == "4h"
