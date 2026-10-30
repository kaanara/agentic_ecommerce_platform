"""ops_policy_versions: validate, save, roll back, and loosening detection for the step-up gate."""

import sys
from pathlib import Path

import pytest

from aecom.config import get_settings
from aecom.policy import versions as pv
from aecom.policy.gate import load_policy

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # render_policy_tab does `from _common import step_up`
sys.path.insert(0, str(DASHBOARD / "app_pages"))


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    """The gate's load_policy() has no args, so it must be busted around every test: otherwise a cached dict from a
    previous test (a different tmp database) could leak into one that never calls save_version/activate itself."""
    load_policy.cache_clear()
    yield
    load_policy.cache_clear()


BASE_YAML = """
defaults:
  unknown_tool_tier: T3
  sandboxed_tool_tier: T3
  approval_ttl_hours: 72
rules:
  send_email:
    tier: T2
    limits:
      amount_eur: 100
  delete_everything:
    tier: T4
  register_vendor:
    tier: T2
    limits:
      monthly_cost_eur: 50
"""


def test_active_yaml_is_none_until_a_version_is_saved():
    assert pv.active_yaml() is None
    assert load_policy() == __import__("yaml").safe_load(get_settings().policy_file.read_text())


def test_validate_yaml_rejects_bad_shapes():
    with pytest.raises(ValueError, match="invalid YAML"):
        pv.validate_yaml("not: valid: yaml: at all: [")
    with pytest.raises(ValueError, match="defaults"):
        pv.validate_yaml("rules: {}")
    with pytest.raises(ValueError, match="rules"):
        pv.validate_yaml("defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}")
    with pytest.raises(ValueError, match="tier"):
        pv.validate_yaml("defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\n"
                         "rules: {send_email: {tier: WAT}}")
    with pytest.raises(ValueError, match="must be a number"):
        pv.validate_yaml("defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\n"
                         "rules: {send_email: {tier: T2, limits: {amount_eur: nope}}}")


def test_save_version_requires_a_name():
    with pytest.raises(ValueError, match="name"):
        pv.save_version(BASE_YAML, "note", "")


def test_save_version_stores_and_activates():
    version_id = pv.save_version(BASE_YAML, "initial rules", "alice", step_up_ok=True)
    assert pv.active_yaml().strip() == BASE_YAML.strip()
    rows = pv.list_versions()
    assert rows[0]["id"] == version_id
    assert rows[0]["active"] is True
    assert rows[0]["created_by"] == "alice"


def test_gate_load_policy_prefers_the_active_version():
    pv.save_version(BASE_YAML, "tighten send_email", "alice", step_up_ok=True)
    policy = load_policy()
    assert policy["rules"]["send_email"]["limits"]["amount_eur"] == 100
    assert policy["rules"]["delete_everything"]["tier"] == "T4"


def test_save_version_rejects_invalid_yaml():
    with pytest.raises(ValueError):
        pv.save_version("not a policy", "oops", "alice")
    assert pv.active_yaml() is None


# --------------------------------------------------------------------------- loosening detection
def test_loosens_detects_lower_tier():
    new_yaml = BASE_YAML.replace("delete_everything:\n    tier: T4", "delete_everything:\n    tier: T2")
    assert pv.loosens(BASE_YAML, new_yaml)


def test_loosens_detects_higher_limit():
    new_yaml = BASE_YAML.replace("amount_eur: 100", "amount_eur: 500")
    assert pv.loosens(BASE_YAML, new_yaml)


def test_loosens_detects_any_t4_rule_change():
    new_yaml = BASE_YAML.replace("delete_everything:\n    tier: T4", "delete_everything:\n    tier: T4\n    reason: x")
    assert pv.loosens(BASE_YAML, new_yaml)


def test_loosens_false_for_a_tightened_or_unchanged_policy():
    tighter = BASE_YAML.replace("amount_eur: 100", "amount_eur: 10")
    assert not pv.loosens(BASE_YAML, tighter)
    assert not pv.loosens(BASE_YAML, BASE_YAML)


def test_loosens_false_for_a_brand_new_rule():
    new_yaml = BASE_YAML + "  brand_new_tool:\n    tier: T0\n"
    assert not pv.loosens(BASE_YAML, new_yaml)


def test_loosens_detects_dropped_monthly_budget_cap():
    with_cap = BASE_YAML.replace("register_vendor:\n    tier: T2\n    limits:\n      monthly_cost_eur: 50",
                                 "register_vendor:\n    tier: T2\n    monthly_budget_setting: vendor_budget_month_eur\n"
                                 "    limits:\n      monthly_cost_eur: 50")
    without_cap = BASE_YAML
    assert pv.loosens(with_cap, without_cap)


def test_loosens_detects_dropped_high_stakes_rule():
    without_t4 = BASE_YAML.replace("  delete_everything:\n    tier: T4\n", "")
    assert pv.loosens(BASE_YAML, without_t4)


def test_save_version_that_loosens_needs_step_up():
    pv.save_version(BASE_YAML, "initial", "alice", step_up_ok=True)
    looser = BASE_YAML.replace("amount_eur: 100", "amount_eur: 500")
    with pytest.raises(PermissionError):
        pv.save_version(looser, "raise the limit", "alice")
    assert pv.active_yaml().strip() == BASE_YAML.strip()
    version_id = pv.save_version(looser, "raise the limit", "alice", step_up_ok=True)
    assert pv.active_yaml() == looser
    assert pv.list_versions()[0]["id"] == version_id


# --------------------------------------------------------------------------- rollback
def test_activate_rolls_back_to_an_older_version():
    first_id = pv.save_version(BASE_YAML, "initial", "alice", step_up_ok=True)
    tighter = BASE_YAML.replace("amount_eur: 100", "amount_eur: 10")
    pv.save_version(tighter, "tighten", "alice")
    assert pv.active_yaml() == tighter

    pv.activate(first_id, "bob", step_up_ok=True)  # rolling back raises the limit again -> itself a loosening
    assert pv.active_yaml().strip() == BASE_YAML.strip()
    rows = {r["id"]: r["active"] for r in pv.list_versions()}
    assert rows[first_id] is True
    assert sum(1 for v in rows.values() if v) == 1  # only one active row ever


def test_activate_unknown_version_raises():
    with pytest.raises(ValueError, match="no policy version"):
        pv.activate(999999, "alice")


def test_activate_that_loosens_needs_step_up():
    tight_id = pv.save_version(BASE_YAML.replace("amount_eur: 100", "amount_eur: 10"), "tight", "alice", step_up_ok=True)
    loose_id = pv.save_version(BASE_YAML, "loose", "alice", step_up_ok=True)
    pv.activate(tight_id, "alice")  # back to the tight one, no step-up needed (not a loosening)
    with pytest.raises(PermissionError):
        pv.activate(loose_id, "alice")
    pv.activate(loose_id, "alice", step_up_ok=True)
    assert pv.active_yaml().strip() == BASE_YAML.strip()


# --------------------------------------------------------------------------- Settings > Policy tab (dashboard)
def _policy_tab_app() -> None:
    from _settings_policy import render_policy_tab

    render_policy_tab("alice")


def test_render_policy_tab_shows_the_active_rules_and_a_yaml_editor():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_policy_tab_app)
    at.run()
    assert not at.exception
    assert at.dataframe  # the rules table, parsed from the file (no version saved yet)
    assert at.text_area[0].value.strip() == get_settings().policy_file.read_text().strip()
    assert any("save" in b.label.lower() for b in at.button)


def test_render_policy_tab_saves_a_tightening_edit_without_step_up():
    from streamlit.testing.v1 import AppTest

    pv.save_version(BASE_YAML, "baseline", "alice", step_up_ok=True)
    tighter = BASE_YAML.replace("amount_eur: 100", "amount_eur: 10")

    at = AppTest.from_function(_policy_tab_app)
    at.run()
    at.text_area[0].set_value(tighter)
    at.text_input[0].set_value("tighten via the dashboard")
    save_button = next(b for b in at.button if "save" in b.label.lower())
    save_button.click()
    at.run()
    assert not at.exception
    assert pv.active_yaml() == tighter


def test_render_policy_tab_parks_a_loosening_save_behind_step_up():
    from streamlit.testing.v1 import AppTest

    from aecom import dashboard_auth as auth

    auth.set_password("a long enough test password", "a long enough test password", "alice")
    pv.save_version(BASE_YAML, "baseline", "alice", step_up_ok=True)
    looser = BASE_YAML.replace("amount_eur: 100", "amount_eur: 500")

    at = AppTest.from_function(_policy_tab_app)
    at.run()
    at.text_area[0].set_value(looser)
    next(b for b in at.button if "save" in b.label.lower()).click()
    at.run()
    assert not at.exception
    assert pv.active_yaml().strip() == BASE_YAML.strip()  # not applied yet - parked behind the password prompt
    assert any("password" in ti.label.lower() for ti in at.text_input)

    at.text_input(key="policy_stepup_pw").set_value("a long enough test password")
    next(b for b in at.button if "confirm" in b.label.lower()).click()
    at.run()
    assert not at.exception
    assert pv.active_yaml() == looser


