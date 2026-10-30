"""Settings page (Govern > Settings): registry-generated forms, Security (dashboard_auth), History, Export/Import.

AppTest renders the real entrypoint (Home.py -> settings.py) so the registry, dashboard_auth and the page code are all
covered together. Secrets are never written to the real .env: `envp` points `envfile.env_path()` at a private tmp file.
"""

import sys
from pathlib import Path

import pytest
import yaml
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

TAB_NAMES = ["AI and models", "Budgets and costs", "Store and email", "Alerts and notifications",
             "Channels", "Chat", "Schedules", "Security", "Policy", "Agents", "Backups and updates", "Autonomy",
             "Advanced", "History", "Export / Import"]

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


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def _set_name(at: AppTest, name: str = "Ada") -> AppTest:
    at.text_input(key="reviewer_name").set_value(name)
    return at.run()


def _has(collection_factory, key: str) -> bool:
    try:
        collection_factory(key=key)
        return True
    except KeyError:
        return False


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


# --------------------------------------------------------------------------- rendering and structure
def test_page_renders_with_login_off(envp):
    at = _app().run()
    assert not at.exception, _messages(at)
    assert at.title[0].value == ":material/settings: Settings"


def _top_tabs(at):
    return [t for t in at.tabs if t.proto.label in TAB_NAMES]


def test_tab_order_matches_the_plan(envp):
    at = _app().run()
    assert [t.proto.label for t in _top_tabs(at)] == TAB_NAMES


def test_policy_agents_backups_tabs_render_their_partials(envp):
    at = _app().run()
    assert not at.exception, _messages(at)
    labels = {t.proto.label for t in at.tabs}
    assert {"Policy", "Agents", "Backups and updates"} <= labels


def test_autonomy_fields_link_to_trust_instead_of_a_raw_field(envp):
    at = _app().run()
    assert not _has(at.number_input, "input_autonomy_min_real_sales")
    captions = " ".join(c.value for t in at.tabs for c in t.get("caption"))
    assert "managed on the **Trust** page" in captions


def test_readonly_advanced_fields_are_disabled(envp):
    at = _app().run()
    assert at.text_input(key="ro_database_url").disabled


# --------------------------------------------------------------------------- generic field editing
def test_saving_a_normal_field_needs_a_name_first(envp):
    at = _app().run()
    at.number_input(key="input_agent_max_steps").set_value(9)
    at.button(key="FormSubmitter:form_agent_max_steps-Save").click().run()
    assert any("name" in w.value for w in at.warning)
    assert reg.value("agent_max_steps") != 9


def test_save_then_reset_a_config_field(envp):
    at = _set_name(_app())
    at.number_input(key="input_agent_max_steps").set_value(9)
    at.button(key="FormSubmitter:form_agent_max_steps-Save").click().run()
    assert not at.exception, _messages(at)
    assert reg.value("agent_max_steps") == 9
    assert reg.source("agent_max_steps") == "ui"
    at.button(key="FormSubmitter:form_agent_max_steps-Reset to default").click().run()
    assert reg.source("agent_max_steps") == "default"


def test_invalid_value_shows_an_inline_error_and_does_not_save(envp):
    at = _set_name(_app())
    at.text_input(key="input_price_opus").set_value("not, numbers")
    at.button(key="FormSubmitter:form_price_opus-Save").click().run()
    assert at.error and "Opus price" in at.error[0].value
    assert reg.source("price_opus") == "default"


def test_search_box_hides_non_matching_fields(envp):
    at = _set_name(_app())
    at.text_input(key="_settings_search").set_value("agent_max_steps")
    at = at.run()
    assert _has(at.number_input, "input_agent_max_steps")
    assert not _has(at.number_input, "input_usd_to_eur")


# --------------------------------------------------------------------------- critical fields need step-up
def test_critical_field_needs_the_dashboard_secret_before_saving(envp):
    at = _set_name(_app(login_off=False))
    at.selectbox(key="input_llm_mode").set_value("anthropic")
    at.button(key="FormSubmitter:form_llm_mode-Save").click().run()
    assert not at.exception, _messages(at)
    assert reg.value("llm_mode") != "anthropic"  # blocked: the step-up prompt showed up instead of saving
    assert at.text_input(key="stepup_llm_mode_pw")
    at.text_input(key="stepup_llm_mode_pw").set_value(TEST_SECRET)
    at.button(key="FormSubmitter:stepup_llm_mode_form-Confirm password").click().run()
    assert not at.exception, _messages(at)
    assert reg.value("llm_mode") == "anthropic"  # confirming applies the parked change without resubmitting the form


def test_wrong_step_up_secret_does_not_apply_the_change(envp):
    at = _set_name(_app(login_off=False))
    at.selectbox(key="input_llm_mode").set_value("anthropic")
    at.button(key="FormSubmitter:form_llm_mode-Save").click().run()
    at.text_input(key="stepup_llm_mode_pw").set_value("totally the wrong one")
    at.button(key="FormSubmitter:stepup_llm_mode_form-Confirm password").click().run()
    assert reg.value("llm_mode") != "anthropic"
    assert any("does not match" in e.value for e in at.error)


# --------------------------------------------------------------------------- secrets: never shown, write-only
def test_secret_shows_only_set_not_set_and_last4_never_the_value(envp, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-verysecretvalue1234")
    secrets.reload()
    at = _app().run()
    body = " ".join(m.value for t in at.tabs for m in t.get("markdown"))
    assert "set" in body and "1234" in body
    assert "verysecretvalue" not in body
    for e in at.text_input:
        assert e.value != "sk-ant-verysecretvalue1234"


def test_test_button_shows_only_for_a_set_secret_and_never_the_value(envp, monkeypatch):
    from aecom.integrations import ping

    calls = []
    monkeypatch.setitem(ping.PINGS, "telegram_bot_token", lambda: calls.append(1) or (True, "Telegram bot reachable"))
    at = _set_name(_app())
    assert not any(b.key == "test_telegram_bot_token" for b in at.button)  # not set yet: no button
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:verysecrettelegramtoken")
    secrets.reload()
    at = _set_name(_app())
    at.button(key="test_telegram_bot_token").click().run()
    assert not at.exception and calls == [1]
    assert any("Telegram bot reachable" in t.value for t in at.toast)
    assert "verysecrettelegramtoken" not in " ".join(str(t.value) for t in at.toast)


def test_saving_a_secret_needs_step_up_and_writes_to_the_private_env_file(envp):
    at = _set_name(_app(login_off=False))
    at.text_input(key="secret_input_sentry_dsn").set_value("https://example.invalid/dsn")
    at.button(key="FormSubmitter:secret_form_sentry_dsn-Save").click().run()
    assert not reg.secret_hint("sentry_dsn")["set"]  # not applied yet: secrets are critical, step-up required
    at.text_input(key="stepup_sentry_dsn_pw").set_value(TEST_SECRET)
    at.button(key="FormSubmitter:stepup_sentry_dsn_form-Confirm password").click().run()
    assert not at.exception, _messages(at)
    assert reg.secret_hint("sentry_dsn")["set"]
    assert "SENTRY_DSN" in envp.read_text()
    assert "https://example.invalid/dsn" in envp.read_text()


# --------------------------------------------------------------------------- Security tab (dashboard_auth only)
def test_set_secret_turns_login_on_and_keeps_the_session_signed_in(envp):
    at = _set_name(_app())  # login off, no secret yet
    fields = {e.label: e for e in at.text_input if e.label in ("New password", "Repeat password")}
    fields["New password"].set_value("a very long new secret 12345")
    fields["Repeat password"].set_value("a very long new secret 12345")
    _button(at, "Set password and turn login on").click().run()
    assert not at.exception, _messages(at)
    assert auth.is_configured() and not auth.disabled()  # the auto-rerun already shows the "login is ON" state
    assert any("ON" in s.value for s in at.success)


def test_change_secret_needs_the_current_one(envp):
    old_secret = "the previous dashboard secret"
    at = _set_name(_app(login_off=False, secret=old_secret))
    fields = {e.label: e for e in at.text_input if e.label in ("Current password", "New password", "Repeat new password")}
    fields["Current password"].set_value("not the right one")
    fields["New password"].set_value("a brand new dashboard secret")
    fields["Repeat new password"].set_value("a brand new dashboard secret")
    _button(at, "Change password").click().run()
    assert any("does not match" in e.value for e in at.error)
    assert not auth.verify_password("a brand new dashboard secret")


def test_turn_login_off_needs_confirmation(envp):
    current_secret = "the dashboard secret in use"
    at = _set_name(_app(login_off=False, secret=current_secret))
    at.text_input(key="turn_off_pw").set_value(current_secret)
    at = _button(at, "Turn login off").click().run()
    assert any(b.label == "Turn off" for b in at.button)  # the confirm_dialog modal is now open
    assert not auth.disabled()  # nothing applied yet: it needs the explicit confirm click (not testable further:
    # AppTest does not simulate st.dialog/st.fragment persisting across a second .run())


# --------------------------------------------------------------------------- History and Export/Import
def test_history_lists_a_change_made_on_the_page(envp):
    at = _set_name(_app())
    at.number_input(key="input_agent_max_steps").set_value(7)
    at.button(key="FormSubmitter:form_agent_max_steps-Save").click().run()
    rows = reg.history(key="agent_max_steps")
    assert rows and rows[0]["by"] == "Ada" and rows[0]["new"] == 7
    at = at.run()
    history_tab = _top_tabs(at)[TAB_NAMES.index("History")]
    assert history_tab.get("dataframe")


def test_export_contains_a_ui_override_but_never_a_secret(envp):
    at = _set_name(_app())
    at.number_input(key="input_agent_max_steps").set_value(11)
    at.button(key="FormSubmitter:form_agent_max_steps-Save").click().run()
    exported = reg.export_settings()
    assert exported.get("agent_max_steps") == 11
    assert "anthropic_api_key" not in exported
    at = at.run()
    export_tab = _top_tabs(at)[TAB_NAMES.index("Export / Import")]
    assert export_tab.get("download_button")
    yaml_text = yaml.safe_dump(exported, sort_keys=True)
    assert "agent_max_steps: 11" in yaml_text
