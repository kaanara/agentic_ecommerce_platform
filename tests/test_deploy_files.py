"""Static checks of the Raspberry Pi deployment files (no systemd needed)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PI = ROOT / "deploy" / "pi"
UPDATE = ROOT / "scripts" / "update.sh"
LONG = ["aecom-scheduler.service", "aecom-api.service", "aecom-dashboard.service"]
UNITS = LONG + ["aecom-backup.service", "aecom-restore.service"]


ONESHOT = {"aecom-backup.service", "aecom-restore.service"}


def _text(name):
    return (PI / name).read_text()


@pytest.mark.parametrize("name", UNITS)
def test_units_have_expected_keys_and_placeholders(name):
    t = _text(name)
    assert "[Unit]" in t
    assert "[Install]" in t or name in ONESHOT  # oneshot units are started on demand
    placeholders = set(re.findall(r"@[A-Z_]+@", t))
    assert placeholders <= {"@USER@", "@APP_DIR@"}
    if name.endswith(".service"):
        assert "User=@USER@" in t
        assert "WorkingDirectory=@APP_DIR@" in t
        assert "ExecStart=/home/@USER@/.local/bin/uv run " in t
        for key in (*(() if name == "aecom-restore.service" else ("NoNewPrivileges=true",)),  # restore needs sudo
                    "PrivateTmp=true", "ProtectControlGroups=true", "ProtectKernelTunables=true", "RestrictSUIDSGID=true", "ReadWritePaths=@APP_DIR@"):
            assert key in t
        assert "After=network-online.target" in t


@pytest.mark.parametrize("name", LONG)
def test_long_running_services_restart(name):
    t = _text(name)
    assert "Restart=always" in t and "RestartSec=5" in t
    assert "StartLimitIntervalSec=" in t and "StartLimitBurst=" in t
    assert "ProtectHome" not in t  # data/ and .venv live under the home directory


def test_bind_addresses_are_local():
    assert "aecom dashboard --host 127.0.0.1 --port 8501" in _text("aecom-dashboard.service")
    assert "aecom api" in _text("aecom-api.service")
    for n in LONG:
        assert "0.0.0.0" not in _text(n)


def test_backup_units():
    s = _text("aecom-backup.service")
    assert "Type=oneshot" in s and re.search(r"aecom backup$", s, re.M)
    r = _text("aecom-restore.service")
    assert "Type=oneshot" in r and "aecom.backup" in r and "[Install]" not in r
    assert not (PI / "aecom-backup.timer").exists()  # the in-app scheduler owns the backup schedule


def test_journald_conf():
    t = _text("journald-aecom.conf")
    assert "SystemMaxUse=200M" in t and "MaxRetentionSec=1month" in t


def test_update_script_static():
    assert shutil.which("bash")
    subprocess.run(["bash", "-n", str(UPDATE)], check=True)
    t = UPDATE.read_text()
    assert "set -euo pipefail" in t
    assert "rm -rf" not in t
    assert ".env" not in t
    backup = t.index("aecom backup --label pre-update")
    assert backup < t.index("git pull --ff-only") < t.index("uv run aecom init")
    assert backup < t.index("uv run aecom init")
    assert "--demo" not in t
    assert "rollback()" in t and "aecom backup restore" in t and "--yes" in t
    for flag in ("--check", "--force", "--no-systemd"):
        assert flag in t


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_update_check_is_a_dry_run(tmp_path):
    origin = tmp_path / "origin"
    _git(tmp_path, "init", "-q", str(origin))
    (origin / "scripts").mkdir()
    shutil.copy(UPDATE, origin / "scripts" / "update.sh")
    (origin / "src" / "aecom").mkdir(parents=True)
    (origin / "src" / "aecom" / "db.py").write_text("# v1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "init")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", f"file://{origin}", str(work))
    (origin / "src" / "aecom" / "db.py").write_text("# v2\n")
    _git(origin, "commit", "-q", "-am", "touch db")

    head = _git(work, "rev-parse", "HEAD")
    r = subprocess.run(["bash", "scripts/update.sh", "--check"], cwd=work, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "1 incoming commit" in r.stdout and "touch db" in r.stdout
    assert "schema change" in r.stdout
    assert _git(work, "rev-parse", "HEAD") == head
    assert _git(work, "status", "--porcelain") == ""


def test_docs_page_mentions_units_and_cloud_trigger():
    t = (ROOT / "docs" / "wiki" / "Raspberry-Pi.md").read_text()
    for n in UNITS + ["journald-aecom.conf", "scripts/update.sh"]:
        assert n in t
    assert "Moving to the cloud" in t and "USD 20-30" in t
    assert "lexoffice" in t and "sevDesk" in t
    assert "update.sh --check" in t and "backup verify" in t and "aecom doctor" in t


def test_update_rollback_is_honest_and_never_discards_new_orders():
    t = (ROOT / "scripts" / "update.sh").read_text()
    body = t[t.index("rollback() {"):t.index("# Services stay stopped")]
    assert "NOT RESTORED" in body and "before-restore" in body
    assert "ROLLBACK INCOMPLETE" in body and "left STOPPED" in body
    assert "if ! svc start" in body
    # doctor gates the service start; no ERR trap (no DB restore) once services run
    assert t.index('STAGE="doctor"') < t.index("trap - ERR\nif ! svc start")
    assert "NOT rolling back" in t
