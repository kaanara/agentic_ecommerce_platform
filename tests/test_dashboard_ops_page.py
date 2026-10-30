"""The Operations page: every tab loads through the real navigation and the page needs a name before acting."""

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import dashboard_auth as auth
from aecom.company_settings import set_setting

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))


def _page() -> AppTest:
    set_setting(auth.MODE_KEY, "off")
    at = AppTest.from_file(str(DASHBOARD / "Home.py"), default_timeout=60)
    at.run()
    at.switch_page("app_pages/operations.py")
    return at.run()


def test_operations_page_renders_all_five_tabs(fresh_company):
    at = _page()
    assert not at.exception
    assert [t.proto.label for t in at.tabs] == ["Run and maintenance", "Memory review", "Tool grants",
                                                "Vendor provisioning", "Incident reports"]


def test_operations_asks_for_a_name_first(fresh_company):
    at = _page()
    assert any("Enter your name" in i.value for i in at.info)
