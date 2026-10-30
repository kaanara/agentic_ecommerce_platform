"""The Accounting page: all four tabs load through the real navigation."""

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import dashboard_auth as auth
from aecom.company_settings import set_setting

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))


def test_accounting_page_renders_its_tabs(fresh_company):
    set_setting(auth.MODE_KEY, "off")
    at = AppTest.from_file(str(DASHBOARD / "Home.py"), default_timeout=60)
    at.run()
    at.switch_page("app_pages/accounting.py")
    at.run()
    assert not at.exception
    assert [t.proto.label for t in at.tabs] == ["Months", "Month-end report", "Payouts", "Accountant export", "Decision review"]
    assert any("Enter your name" in i.value for i in at.info)
