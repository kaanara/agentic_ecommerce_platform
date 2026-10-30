"""Every dashboard page renders without an exception (AppTest), with the login off and on.

Pages are exercised through the real entrypoint (Home.py -> st.navigation) so the router, the gate and the page code are
all covered. Timed fragments (run_every) are not part of the smoke path.
"""

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from aecom import dashboard_auth as auth
from aecom.company_settings import set_setting

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # pages do `from _common import ...`
import _nav  # noqa: E402

HOME = str(DASHBOARD / "Home.py")
PAGE_FILES = [d.file for d in _nav.available()]


def _app(login_off: bool = True) -> AppTest:
    if login_off:
        set_setting(auth.MODE_KEY, "off")
    else:
        auth.reset_password_local("a long test password")
    return AppTest.from_file(HOME, default_timeout=60)


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def test_entrypoint_renders_default_page_with_login_off():
    at = _app().run()
    assert not at.exception, _messages(at)
    assert any("Dashboard login is off" in w.value for w in at.warning)


def test_login_form_when_login_is_on_and_nothing_else():
    at = _app(login_off=False).run()
    assert not at.exception, _messages(at)
    assert at.text_input and at.text_input[0].label == "Password"
    assert not at.dataframe and not at.metric  # nothing behind the gate is rendered


def test_correct_password_opens_the_dashboard():
    at = _app(login_off=False).run()
    at.text_input[0].set_value("a long test password")
    at.button[0].click()
    at.run()
    assert not at.exception, _messages(at)
    assert not at.text_input or at.text_input[0].label != "Password"


@pytest.mark.parametrize("page", PAGE_FILES)
def test_every_page_renders(page):
    at = _app().run()
    at.switch_page(f"app_pages/{page}").run()
    assert not at.exception, f"{page}: {_messages(at)}"
    assert at.title, f"{page} rendered no title"
