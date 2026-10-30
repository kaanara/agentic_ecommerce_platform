"""Govern > Operations: vendor provisioning (E5/E6/E7) and incident reports (K8/K10), both human-only tabs.

AppTest.from_function wraps a plain call to each render function (operations.py itself is the lead's page and is
not under test here) - the pattern used by test_dashboard_ops_review.py and test_backup_encryption.py. Every test
gets a fresh temp DB from tests/conftest.py's `fresh_company` fixture (autouse).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from aecom import incident_export, incidents, provisioning
from aecom.db import Vendor, now, session

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partials do `from _common import ...`


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


# --------------------------------------------------------------------------- vendor provisioning tab
def _make_vendor(name: str = "Acme Ads", credential_env: str = "ACME_ADS_TOKEN", checklist=None,
                 activation=None, verified_at=None, last_check_error=None) -> None:
    with session() as s:
        s.add(Vendor(name=name, category="ads", monthly_cost_eur=12.0, status="provisioning",
                     credential_env=credential_env,
                     checklist=checklist if checklist is not None else
                     [{"step": "Create the account", "done": False}, {"step": "Put it in .env", "done": False}],
                     activation=activation, verified_at=verified_at, last_check_error=last_check_error))


def _prov_tab_named():
    from _ops_provisioning import render_provisioning_tab

    render_provisioning_tab("Ada")


def _prov_tab_blank():
    from _ops_provisioning import render_provisioning_tab

    render_provisioning_tab("")


def test_provisioning_missing_name_blocks_and_shows_nothing():
    _make_vendor()
    at = AppTest.from_function(_prov_tab_blank).run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert not at.button  # no checklist/snooze/confirm controls rendered
    assert provisioning.list_provisioning()[0]["checklist"][0]["done"] is False  # nothing was touched


def test_provisioning_empty_state_when_nothing_waits():
    at = AppTest.from_function(_prov_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("Nothing waiting for provisioning" in m.value for m in at.markdown)


def test_provisioning_checklist_is_shown_with_open_steps():
    _make_vendor()
    at = AppTest.from_function(_prov_tab_named).run()
    assert not at.exception, _messages(at)
    markdown = " ".join(m.value for m in at.markdown)
    assert "Acme Ads" in markdown
    captions = " ".join(c.value for c in at.caption)
    assert "ads" in captions
    assert at.button(key="_prov_done_Acme Ads_1")
    assert at.button(key="_prov_done_Acme Ads_2")
    assert not any(b.key == "_prov_confirm_Acme Ads" for b in at.button)  # not waiting for confirmation yet


def test_provisioning_tick_marks_the_step_done():
    _make_vendor()
    at = AppTest.from_function(_prov_tab_named).run()
    at.button(key="_prov_done_Acme Ads_1").click().run()
    assert not at.exception, _messages(at)
    row = provisioning.list_provisioning()[0]
    assert [c["done"] for c in row["checklist"]] == [True, False]
    # the done step no longer offers "Mark done" (the control re-renders after the rerun)
    assert not any(b.key == "_prov_done_Acme Ads_1" for b in at.button)
    assert any(b.key == "_prov_done_Acme Ads_2" for b in at.button)


def test_provisioning_snooze_defers_the_next_reminder():
    import datetime as dt

    _make_vendor()
    before = now()
    at = AppTest.from_function(_prov_tab_named).run()
    at.number_input(key="_prov_snooze_days_Acme Ads").set_value(5)
    at.button(key="_prov_snooze_Acme Ads").click().run()
    assert not at.exception, _messages(at)
    # last_reminded_at was pushed roughly 5 days out; the render + its success message immediately rerun, so only
    # the resulting state (not the transient message) survives in the final tree
    row = provisioning.list_provisioning()[0]
    assert row["last_reminded_at"] >= before + dt.timedelta(days=5)


def test_provisioning_confirm_button_only_offered_when_waiting_for_confirmation():
    _make_vendor(verified_at=None)  # credential not verified yet
    at = AppTest.from_function(_prov_tab_named).run()
    assert not at.exception, _messages(at)
    assert not any(b.key == "_prov_confirm_Acme Ads" for b in at.button)


def test_provisioning_confirm_activates_the_vendor(monkeypatch):
    monkeypatch.setenv("ACME_ADS_TOKEN", "a-working-credential-value")
    _make_vendor(verified_at=now())  # activation defaults to human_confirm for an unknown vendor (U3)
    assert provisioning.list_provisioning()[0]["waiting_for_your_confirmation"] is True
    at = AppTest.from_function(_prov_tab_named).run()
    assert any(b.key == "_prov_confirm_Acme Ads" for b in at.button)
    at.button(key="_prov_confirm_Acme Ads").click().run()
    assert not at.exception, _messages(at)
    assert provisioning.list_provisioning() == []  # no longer "provisioning": it is active now
    with session() as s:
        assert s.scalars(select(Vendor)).one().status == "active"


def test_provisioning_last_check_error_is_shown_but_never_the_credential(monkeypatch):
    monkeypatch.setenv("ACME_ADS_TOKEN", "super-secret-value-123")
    _make_vendor(last_check_error="HTTP 401 Unauthorized")
    at = AppTest.from_function(_prov_tab_named).run()
    assert not at.exception, _messages(at)
    body = " ".join(w.value for w in at.warning)
    assert "HTTP 401" in body
    assert "super-secret-value-123" not in body


# --------------------------------------------------------------------------- incident reports tab
@pytest.fixture(autouse=True)
def _issues_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(incident_export, "ISSUES_DIR", tmp_path / "reports" / "issues")
    return tmp_path / "reports" / "issues"


def _make_incident(monitor: str = "undelivered_order", severity: str = "P1", subject: str = "ord_abc123456789",
                   detail: str = "order stuck") -> str:
    inc, _ = incidents.raise_incident(monitor, severity, subject, detail)
    return inc["id"]


def _inc_tab_named():
    from _ops_incidents import render_incident_reports_tab

    render_incident_reports_tab("Ada")


def _inc_tab_blank():
    from _ops_incidents import render_incident_reports_tab

    render_incident_reports_tab("")


def test_incidents_missing_name_blocks_and_shows_nothing():
    _make_incident()
    at = AppTest.from_function(_inc_tab_blank).run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert not at.selectbox and not at.code


def test_incidents_empty_state_when_there_are_none():
    at = AppTest.from_function(_inc_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("No incidents to report on" in m.value for m in at.markdown)


def test_incidents_preview_is_shown_and_masked(monkeypatch):
    monkeypatch.setenv("SHOP_API_KEY", "zz-very-secret-api-value-42")
    inc_id = _make_incident(detail="store said no; key zz-very-secret-api-value-42")
    at = AppTest.from_function(_inc_tab_named).run()
    assert not at.exception, _messages(at)
    assert at.selectbox(key="_inc_select").options  # the incident is listed
    body = at.code[0].value
    assert inc_id in body and "## Timeline" in body
    assert "zz-very-secret-api-value-42" not in body


def test_incidents_write_issue_file_creates_the_report(tmp_path):
    inc_id = _make_incident()
    at = AppTest.from_function(_inc_tab_named).run()
    at.button(key=f"_inc_export_{inc_id}").click().run()
    assert not at.exception, _messages(at)
    path = incident_export.ISSUES_DIR / f"{inc_id}.md"
    assert path.exists()
    assert any(str(path) in s.value for s in at.success)


def test_incidents_draft_postmortem_requires_resolved_p1():
    inc_id = _make_incident(severity="P1")
    at = AppTest.from_function(_inc_tab_named).run()
    at.button(key=f"_inc_postmortem_{inc_id}").click().run()
    assert not at.exception, _messages(at)
    assert any("resolve it" in e.value for e in at.error)

    incidents.resolve(inc_id, "Ada")
    at = AppTest.from_function(_inc_tab_named).run()
    at.button(key=f"_inc_postmortem_{inc_id}").click().run()
    assert not at.exception, _messages(at)
    assert any("drafted" in s.value for s in at.success)


def test_incidents_live_option_hidden_without_a_token():
    _make_incident()
    at = AppTest.from_function(_inc_tab_named).run()
    assert not at.exception, _messages(at)
    assert not any("live" in b.label.lower() for b in at.button)
    assert "Create the GitHub issue (live)" not in " ".join(m.value for m in at.markdown)


def test_incidents_live_option_shown_with_a_token_and_gated(monkeypatch):
    from aecom import dashboard_auth as auth

    auth.reset_password_local("a long test secret")
    monkeypatch.setenv("GITHUB_REPO", "acme/shop")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_live_token_value")
    inc_id = _make_incident()
    at = AppTest.from_function(_inc_tab_named).run()
    assert not at.exception, _messages(at)
    assert "Create the GitHub issue (live)" in " ".join(m.value for m in at.markdown)
    # the live button needs step-up first: it is not offered until the password form is confirmed
    assert not any(b.key == f"_inc_live_btn_{inc_id}" for b in at.button)
    pw_key = f"_inc_live_stepup_{inc_id}_pw"
    assert at.text_input(key=pw_key)  # the step-up password form is shown instead

    at.text_input(key=pw_key).set_value("a long test secret")
    at.button(key=f"FormSubmitter:_inc_live_stepup_{inc_id}_form-Confirm password").click().run()
    assert not at.exception, _messages(at)
    assert at.button(key=f"_inc_live_btn_{inc_id}")  # now offered, but still disabled until the id is typed
    assert at.button(key=f"_inc_live_btn_{inc_id}").disabled
