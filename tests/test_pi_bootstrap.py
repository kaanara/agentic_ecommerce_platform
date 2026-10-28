"""scripts/pi-bootstrap.sh: syntax, safety of the dry run, and what it promises not to do."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pi-bootstrap.sh"


def dry_run(tmp_path, *flags):
    """Run the script in dry-run mode with a fake sudo/apt-get that would leave a marker file if ever called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "side-effect-marker"
    for name in ("sudo", "apt-get", "systemctl", "ufw", "visudo", "curl", "git"):
        f = bin_dir / name
        f.write_text(f"#!/bin/sh\necho {name} >> '{marker}'\n")
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir()
    r = subprocess.run(["bash", str(SCRIPT), "--dry-run", "--app-dir", str(ROOT), *flags],
                       capture_output=True, text=True, env=env, timeout=60)
    return r, marker


def test_script_has_valid_bash_syntax_and_strict_mode():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert "rm -rf" not in text


def test_dry_run_changes_nothing_and_never_calls_sudo(tmp_path):
    r, marker = dry_run(tmp_path, "--allow-update-sudo", "--firewall", "--enable-services")
    assert r.returncode == 0, r.stderr
    assert not marker.exists(), f"a dry run executed: {marker.read_text()}"
    for step in ("preflight", "system packages", "uv", "project code", "dependencies", ".env", "database", "systemd units", "done"):
        assert step in r.stdout
    assert not (tmp_path / "home" / ".local").exists()


def test_services_firewall_and_sudo_rules_are_opt_in(tmp_path):
    r, _ = dry_run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "systemctl enable" not in r.stdout and "ufw" not in r.stdout and "sudoers" not in r.stdout
    assert "chmod 600 .env" in r.stdout


def test_update_sudo_rule_lists_exact_commands_without_wildcards(tmp_path):
    r, _ = dry_run(tmp_path, "--allow-update-sudo")
    rule = next(l for l in r.stdout.splitlines() if "NOPASSWD:" in l)
    assert "*" not in rule
    for svc in ("aecom-scheduler", "aecom-api", "aecom-dashboard"):
        assert f"start {svc}," in rule + "," and f"stop {svc}" in rule  # exactly what scripts/update.sh runs


def test_script_never_reads_or_prints_the_env_file():
    text = SCRIPT.read_text()
    for forbidden in ("cat .env", "cat \"$APP_DIR/.env\"", "source .env", ". .env", "grep .env", "< .env", "env | "):
        assert forbidden not in text
    assert 'printf' not in text.split("cp .env.example")[1].split("\n")[0]  # nothing echoes the file contents


def test_root_is_refused():
    text = SCRIPT.read_text()
    assert '"$(id -u)" -ne 0' in text and "not root" in text


def test_guide_documents_the_script_and_uses_exact_sudo_rules():
    guide = (ROOT / "docs" / "wiki" / "Raspberry-Pi.md").read_text()
    assert "scripts/pi-bootstrap.sh" in guide
    rules = [l for l in guide.splitlines() if "NOPASSWD:" in l]
    assert rules and all("*" not in l for l in rules)  # the actual rule lists exact commands, no wildcard
