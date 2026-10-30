"""Overview (Operate) and Health (Govern) pages.

AppTest renders the real entrypoint (Home.py -> overview.py / health.py) so the gate, navigation and page code are
all covered together, the same pattern as test_dashboard_smoke.py.
"""

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import backup, doctor, incidents
from aecom import dashboard_auth as auth
from aecom.company_settings import enter_safe_mode, kill_switch_on, set_setting
from aecom.conversations import append_message, create
from aecom.db import Approval, EventRow, session

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the pages do `from _common import ...`
HOME = str(DASHBOARD / "Home.py")


def _app(page: str) -> AppTest:
    set_setting(auth.MODE_KEY, "off")
    at = AppTest.from_file(HOME, default_timeout=60)
    at.run()
    at.switch_page(f"app_pages/{page}")
    return at.run()


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def _set_name(at: AppTest, name: str = "Ada") -> AppTest:
    at.text_input(key="reviewer_name").set_value(name)
    return at.run()


def _button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def _seed_pending_approval() -> str:
    with session() as s:
        a = Approval(agent_id="cfo", tool_name="refund_order", args={"x": 1}, risk_tier="T2", reason="test")
        s.add(a)
        s.flush()
        return a.id


def _seed_open_incident(monitor: str = "stuck_run", subject: str = "run-1") -> str:
    incident, _ = incidents.raise_incident(monitor, "P2", subject, "a run is stuck")
    return incident["id"]


def _seed_unread_chat() -> str:
    conv_id = create("support", owner="owner", started_by="agent")
    append_message(conv_id, "assistant", "need a decision", agent_id="support")
    return conv_id


def _seed_dead_letter(topic: str = "order.placed") -> int:
    with session() as s:
        row = EventRow(topic=topic, payload={}, source="test", attempts=5, dead=True, last_error="boom: too many retries")
        s.add(row)
        s.flush()
        return row.id


# --------------------------------------------------------------------------------------------------- smoke
def test_overview_renders_with_login_off():
    at = _app("overview.py")
    assert not at.exception, _messages(at)
    assert at.title[0].value == ":material/dashboard: Overview"


def test_health_renders_with_login_off():
    at = _app("health.py")
    assert not at.exception, _messages(at)
    assert at.title[0].value == ":material/monitor_heart: Health"


# --------------------------------------------------------------------------------------------- overview: banner
def test_overview_empty_state_when_nothing_needs_attention():
    at = _app("overview.py")
    assert any("Nothing needs attention" in m.value for m in at.markdown)


def test_overview_needs_attention_lists_pending_approvals_incidents_and_unread_chat():
    _seed_pending_approval()
    _seed_open_incident()
    _seed_unread_chat()
    at = _app("overview.py")
    assert not at.exception, _messages(at)
    text = " ".join(m.value for m in at.markdown)
    assert "1 approval(s) waiting" in text
    assert "1 open incident(s)" in text
    assert "1 unread message(s)" in text
    labels = {link.label for link in at.get("page_link")}
    assert {"Approvals", "Health", "Chat"} <= labels


def test_overview_safe_mode_banner_and_leave_button():
    enter_safe_mode("a monitor fired")
    at = _set_name(_app("overview.py"))
    assert any("Safe mode is ON" in e.value for e in at.error)
    assert any("Leave safe mode" in b.label for b in at.button)
    text = " ".join(m.value for m in at.markdown)
    assert "Safe mode is ON" in text  # also listed under "needs attention"


def test_overview_kill_switch_button_opens_confirm_dialog_without_toggling():
    at = _set_name(_app("overview.py"))
    assert not kill_switch_on()
    at = _button(at, "Turn kill switch ON").click().run()
    assert any(b.label == "Turn on" for b in at.button)  # the confirm_dialog modal is open
    assert not kill_switch_on()  # nothing applied until the modal is confirmed


def test_overview_pause_button_requires_a_reviewer_name():
    at = _app("overview.py")
    assert _button(at, "Pause agents").disabled
    at = _set_name(at)
    assert not _button(at, "Pause agents").disabled


def test_overview_shortcuts_link_to_every_section():
    at = _app("overview.py")
    labels = {link.label for link in at.get("page_link")}
    assert {"Chat", "Approvals", "Business", "Health", "Settings"} <= labels


# --------------------------------------------------------------------------------------------- health: incidents
def test_health_no_open_incidents_shows_empty_state():
    at = _app("health.py")
    assert any("No open incidents" in m.value for m in at.markdown)


def test_health_open_incident_card_and_acknowledge():
    incident_id = _seed_open_incident()
    at = _set_name(_app("health.py"))
    assert any("stuck_run" in m.value for m in at.markdown)
    assert any(":orange-badge[P2]" in m.value for m in at.markdown)
    at = _button(at, "Acknowledge").click().run()
    assert not at.exception, _messages(at)
    assert incidents.get_incident(incident_id)["status"] == "acknowledged"


def test_health_resolve_clears_the_incident():
    incident_id = _seed_open_incident()
    at = _set_name(_app("health.py"))
    at = _button(at, "Resolve").click().run()
    assert not at.exception, _messages(at)
    assert incidents.get_incident(incident_id)["status"] == "resolved"


def test_health_incident_buttons_disabled_without_a_reviewer_name():
    _seed_open_incident()
    at = _app("health.py")
    assert _button(at, "Acknowledge").disabled
    assert _button(at, "Resolve").disabled


# --------------------------------------------------------------------------------------- health: monitors / bus
def test_health_monitor_activity_table_reflects_incident_history():
    _seed_open_incident("stuck_run", "run-1")
    at = _app("health.py")
    assert not at.exception, _messages(at)
    rows = at.dataframe[0].value.to_dict("records")
    assert any(r["monitor"] == "stuck_run" and r["status"] == "P2" for r in rows)


def test_health_dead_letters_and_retry():
    event_id = _seed_dead_letter()
    at = _app("health.py")
    assert not at.exception, _messages(at)
    at.multiselect[0].set_value([event_id])
    at.run()
    at = _button(at, "Retry selected").click().run()
    assert not at.exception, _messages(at)
    # the page reruns right after a successful retry (st.rerun()), so the success toast itself is not observable;
    # the dead-letter table clearing (and the row's dead flag) is the real, persistent effect.
    assert any("No dead-lettered events" in m.value for m in at.markdown)
    with session() as s:
        row = s.get(EventRow, event_id)
        assert not row.dead


# --------------------------------------------------------------------------------------------- health: doctor
def test_health_doctor_button_runs_a_report():
    at = _app("health.py")
    assert not any("ok" == m.label for m in at.metric)
    at = _button(at, "Run doctor checks").click().run()
    assert not at.exception, _messages(at)
    labels = {m.label for m in at.metric}
    assert {"ok", "warn", "fail", "version"} <= labels
    report = doctor.report()
    assert sum(int(m.value) for m in at.metric if m.label in ("ok", "warn", "fail")) == len(report["checks"])


# --------------------------------------------------------------------------------------------- health: backup
def test_health_backup_status_shows_the_newest_backup(monkeypatch, tmp_path):
    from aecom.config import get_settings

    monkeypatch.setattr(get_settings(), "backup_dir", str(tmp_path))
    path = backup.create_backup(label="smoke")
    at = _app("health.py")
    assert not at.exception, _messages(at)
    values = {m.label: m.value for m in at.metric}
    assert values["Last backup"] == path.name
    assert values["Verified"] == "no"


def test_health_deployment_revision_is_shown():
    at = _app("health.py")
    values = {m.label: m.value for m in at.metric}
    assert values["Deployed revision"] == doctor.version()
