"""`aecom update-status record` and the Health page's last-update display."""

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import cli, jobs
from aecom import dashboard_auth as auth
from aecom.company_settings import set_setting

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))


def _health() -> AppTest:
    set_setting(auth.MODE_KEY, "off")
    at = AppTest.from_file(str(DASHBOARD / "Home.py"), default_timeout=60)
    at.run()
    at.switch_page("app_pages/health.py")
    return at.run()


def test_record_writes_an_update_job(fresh_company):
    cli.main(["update-status", "record", "--status", "rolled-back", "--stage", "doctor", "--from", "abc", "--to", "def"])
    (row,) = jobs.list_jobs(kind="update")
    assert row["status"] == "rolled-back" and row["requested_by"] == "update.sh"
    assert row["result"] == {"status": "rolled-back", "stage": "doctor", "from": "abc", "to": "def"}


def test_health_says_when_no_update_was_recorded(fresh_company):
    at = _health()
    assert not at.exception
    assert any("No `scripts/update.sh` run" in c.value for c in at.caption)


def test_health_shows_the_last_update(fresh_company):
    jobs.record("update", "update.sh", {"status": "success", "stage": "", "from": "aaa111", "to": "bbb222"}, status="success")
    at = _health()
    assert not at.exception
    values = {m.label: m.value for m in at.metric}
    assert values["Last update"] == "success" and values["From"] == "aaa111" and values["To"] == "bbb222"
