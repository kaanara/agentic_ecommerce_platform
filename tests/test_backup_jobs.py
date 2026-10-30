"""§5c: backup.requested / backup.verify_requested -> ops_jobs, and the restore-apply one-shot entrypoint.

All on tmp databases and a tmp BACKUP_DIR; no network (alerts' httpx calls are monkeypatched away via `net`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from aecom import alerts, backup, jobs
from aecom.bootstrap import seed_demo
from aecom.bus.events import drain, publish, status
from aecom.company_settings import get_setting, set_setting
from aecom.config import get_settings
from aecom.store.adapter import get_store

SKU = "LIC-FOCUSFLOW-PRO-V1"
T0 = datetime(2026, 10, 28, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
def bk(tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(get_settings(), "backup_dir", d)
    monkeypatch.setattr(get_settings(), "backup_target", None)
    monkeypatch.setattr(get_settings(), "backup_verify_after", False)
    return d


@pytest.fixture
def demo():
    seed_demo()
    get_store().create_order("c@example.com", SKU, "DE")
    drain()


@pytest.fixture(autouse=True)
def net(monkeypatch):
    """No real HTTP from alerts.route (the restore-apply entrypoint alerts on completion)."""
    monkeypatch.setattr(alerts.httpx, "post", lambda *a, **k: httpx.Response(200, request=httpx.Request("POST", a[0])))
    monkeypatch.setattr(alerts.httpx, "get", lambda *a, **k: httpx.Response(200, request=httpx.Request("GET", a[0])))


# --- handlers registered on `jobs` ------------------------------------------------------------------------------

def test_backup_job_creates_rotates_and_is_registered(demo, bk):
    assert "backup" in jobs._handlers and "verify" in jobs._handlers
    result = backup.run_backup_job({})
    assert backup.Path(result["backup"]).is_dir()
    assert result["rotated"] == []
    assert "copied_off_device" not in result  # no backup_target configured
    assert "verified" not in result  # backup_verify_after is off in this fixture


def test_backup_job_verifies_after_when_enabled(demo, bk, monkeypatch):
    monkeypatch.setattr(get_settings(), "backup_verify_after", True)
    result = backup.run_backup_job({"label": "nightly"})
    assert result["verified"] is True and result["verify_problems"] == []
    m = backup.read_manifest(backup.Path(result["backup"]))
    assert m["verified"] is True and "verified_at" in m


def test_backup_job_records_off_device_failure_without_raising(demo, bk, monkeypatch):
    monkeypatch.setattr(get_settings(), "backup_target", "pi@nas:/srv/aecom")

    def boom(*a, **k):
        raise backup.subprocess.CalledProcessError(23, "rsync")

    monkeypatch.setattr(backup.subprocess, "run", boom)
    result = backup.run_backup_job({})
    assert result["copied_off_device"] is False and "target_error" in result
    m = backup.read_manifest(backup.Path(result["backup"]))
    assert m["copied_off_device"] is False


def test_verify_job_verifies_newest_and_updates_manifest(demo, bk):
    p = backup.create_backup(now=T0)
    report = backup.run_verify_job({})
    assert report["ok"] is True
    assert backup.read_manifest(p)["verified"] is True


def test_verify_job_named_backup(demo, bk):
    backup.create_backup(now=T0)
    p2 = backup.create_backup(now=T0.replace(hour=4))
    report = backup.run_verify_job({"backup": p2.name})
    assert report["ok"] and report["path"] == str(p2)


def test_verify_job_raises_when_nothing_to_verify(bk):
    with pytest.raises(backup.BackupError, match="no backups"):
        backup.run_verify_job({})


def test_verify_job_reports_failure_without_raising(demo, bk):
    p = backup.create_backup(now=T0)
    with (p / "aecom.db").open("r+b") as f:
        f.seek(300)
        f.write(b"\xff" * 64)
    report = backup.run_verify_job({})
    assert report["ok"] is False and report["problems"]


# --- events -> ops_jobs (the dashboard only ever publishes) -----------------------------------------------------

def test_backup_requested_event_enqueues_and_runs_through_run_pending(demo, bk):
    publish("backup.requested", {"requested_by": "kaan", "label": "manual"})
    assert status()["queued"] == 1
    drain()  # delivers the event: the subscriber enqueues an ops_jobs row
    assert jobs.get(jobs.list_jobs("backup")[0]["id"])["status"] == "queued"
    jobs.run_pending()  # the scheduler's tick (see the scheduler.py patch snippet)
    jobs_run = jobs.list_jobs("backup")
    assert jobs_run and jobs_run[0]["status"] == "ok" and jobs_run[0]["requested_by"] == "kaan"
    assert backup.Path(jobs_run[0]["result"]["backup"]).is_dir()


def test_verify_requested_event_enqueues_and_runs(demo, bk):
    backup.create_backup(now=T0)
    publish("backup.verify_requested", {"requested_by": "mara", "backup": None})
    drain()
    jobs.run_pending()
    jobs_run = jobs.list_jobs("verify")
    assert jobs_run and jobs_run[0]["status"] == "ok" and jobs_run[0]["result"]["ok"] is True


# --- restore-apply one-shot entrypoint (aecom-restore.service) --------------------------------------------------

def test_restore_apply_restores_verifies_runs_doctor_and_clears_request(demo, bk):
    p = backup.create_backup(now=T0)
    set_setting("restore_requested", p.name)
    result = backup.restore_apply_from_settings(now=T0.replace(hour=5))
    assert "error" not in result
    assert result["verify"]["ok"] is True
    assert backup.Path(result["pre_restore_backup"]).is_dir()
    assert result["restore"]["restored"]
    assert result["doctor"]
    assert get_setting("restore_requested") == ""
    jobs_run = jobs.list_jobs("restore")
    assert jobs_run and jobs_run[0]["status"] == "ok"


def test_restore_apply_refuses_a_damaged_backup_and_records_error(demo, bk):
    p = backup.create_backup(now=T0)
    with (p / "aecom.db").open("r+b") as f:
        f.seek(300)
        f.write(b"\xff" * 64)
    set_setting("restore_requested", p.name)
    result = backup.restore_apply_from_settings(now=T0.replace(hour=5))
    assert "error" in result and "verification" in result["error"]
    assert get_setting("restore_requested") == ""
    jobs_run = jobs.list_jobs("restore")
    assert jobs_run and jobs_run[0]["status"] == "error"


def test_restore_apply_with_no_request_is_a_no_op_error(bk):
    result = backup.restore_apply_from_settings()
    assert "error" in result
    jobs_run = jobs.list_jobs("restore")
    assert jobs_run and jobs_run[0]["status"] == "error"


def test_retention_overrides_from_settings_when_present(monkeypatch):
    class FakeSettings:
        backup_keep_daily = 2
        backup_keep_weeks = 0
        backup_keep_labelled = 1

    monkeypatch.setattr(backup, "get_settings", lambda: FakeSettings())
    names = ["20261028-030000", "20261027-030000", "20261026-030000"]
    deleted = backup.select_deletions(names, datetime(2026, 10, 28, 12, 0))
    assert deleted == ["20261026-030000"]  # only the newest 2 (keep_daily=2) kept, weeks/labels off
