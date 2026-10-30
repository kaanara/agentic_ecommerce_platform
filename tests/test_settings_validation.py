"""N4/N6: the 5 grouped Settings tabs, the live password validation and the backup passphrase minimum length.

Covers the roadmap items that `tests/test_dashboard_settings.py` does not already own: tab grouping/structure, the
live messages under the Security tab's new/repeat password fields (with the Set/Change button disabled until both
are valid), and `settings_registry.set_value` refusing a short `backup_passphrase`.
"""

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from aecom import dashboard_auth as auth
from aecom import envfile
from aecom import settings_registry as reg
from aecom.company_settings import set_setting
from aecom.config import get_settings
from aecom.secrets import secrets

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the page does `from _common import ...`
HOME = str(DASHBOARD / "Home.py")

GROUP_NAMES = list(reg.GROUPS.keys())
TEST_SECRET = "a long test secret"  # 12+ chars, meets dashboard_auth.MIN_PASSWORD_LENGTH


@pytest.fixture
def envp(tmp_path, monkeypatch):
    """A private .env, so a secret saved through the page never touches the real one; also resets the registry's
    in-memory override state, which otherwise outlives this test's database."""
    path = tmp_path / ".env"
    monkeypatch.setattr(envfile, "env_path", lambda: path)
    secrets.reload()
    reg._overridden.clear()
    reg._seen_version = None
    settings = get_settings()
    saved = {k: getattr(settings, k) for k in type(settings).model_fields}
    yield path
    for k, v in saved.items():
        setattr(settings, k, v)
    reg._overridden.clear()
    reg._seen_version = None
    secrets.reload()


def _login(at: AppTest, secret: str) -> AppTest:
    at.text_input[0].set_value(secret)
    at.button[0].click()
    return at.run()


def _app(login_off: bool = True, secret: str = TEST_SECRET) -> AppTest:
    if login_off:
        set_setting(auth.MODE_KEY, "off")
    else:
        auth.reset_password_local(secret)
    at = AppTest.from_file(HOME, default_timeout=60)
    at.run()
    if not login_off:
        at = _login(at, secret)
    at.switch_page("app_pages/settings.py")
    return at


def _set_name(at: AppTest, name: str = "Ada") -> AppTest:
    at.text_input(key="reviewer_name").set_value(name)
    return at.run()


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _captions(at: AppTest) -> str:
    return " ".join(str(c.value) for t in at.tabs for c in t.get("caption"))


# --------------------------------------------------------------------------- 1) the 5 grouped top-level tabs
ALL_LABELS = set(GROUP_NAMES) | {name for names in reg.GROUPS.values() for name in names}


def test_settings_has_5_top_level_groups_with_the_sections_nested_inside(envp):
    at = _app().run()
    assert not at.exception
    # Document order (depth-first) puts each group's label right before its own nested sections (a partial, e.g.
    # Agents, may render further tabs of its own deeper still - those have labels outside ALL_LABELS and are
    # filtered out here, the same way the page's own tests do it).
    labels = [t.proto.label for t in at.tabs if t.proto.label in ALL_LABELS]
    i = 0
    for group_name in GROUP_NAMES:
        assert labels[i] == group_name
        i += 1
        for section in reg.GROUPS[group_name]:
            assert labels[i] == section
            i += 1
    assert i == len(labels)


def test_find_a_setting_search_box_is_labelled_and_has_example_placeholder(envp):
    at = _app().run()
    search = at.text_input(key="_settings_search")
    assert search.label == "Find a setting"
    assert search.proto.placeholder and "e.g." in search.proto.placeholder


# --------------------------------------------------------------------------- 3) live password validation
def test_short_new_password_shows_a_live_character_count_message(envp):
    at = _set_name(_app())  # login off: the "set password" form
    at.text_input(key="set_pw_new").set_value("short")
    at = at.run()
    assert "Use at least 12 characters (you have 5)." in _captions(at)
    assert _button(at, "Set password and turn login on").disabled


def test_mismatched_passwords_show_a_live_message(envp):
    at = _set_name(_app())
    at.text_input(key="set_pw_new").set_value("a perfectly long enough password")
    at.text_input(key="set_pw_repeat").set_value("a different long enough password")
    at = at.run()
    assert "The two passwords are different. Type the same one twice." in _captions(at)
    assert _button(at, "Set password and turn login on").disabled


def test_matching_long_passwords_enable_the_set_button(envp):
    at = _set_name(_app())
    at.text_input(key="set_pw_new").set_value("a perfectly long enough password")
    at.text_input(key="set_pw_repeat").set_value("a perfectly long enough password")
    at = at.run()
    assert not _button(at, "Set password and turn login on").disabled


def test_change_password_button_disabled_until_the_new_pair_is_valid(envp):
    at = _set_name(_app(login_off=False))  # login on: the "change password" form
    assert _button(at, "Change password").disabled  # nothing typed yet
    at.text_input(key="change_pw_new").set_value("another perfectly long password")
    at.text_input(key="change_pw_repeat").set_value("another perfectly long password")
    at = at.run()
    assert not _button(at, "Change password").disabled


# --------------------------------------------------------------------------- 4) backup passphrase minimum length
def test_short_backup_passphrase_is_refused_with_the_12_character_message(envp):
    with pytest.raises(ValueError, match="Use at least 12 characters for the backup passphrase."):
        reg.set_value("backup_passphrase", "too short", "Ada", step_up_ok=True)


def test_12_character_backup_passphrase_is_accepted(envp):
    assert reg.set_value("backup_passphrase", "exactly 12ch", "Ada", step_up_ok=True) is True
    assert reg.secret_hint("backup_passphrase")["set"]
