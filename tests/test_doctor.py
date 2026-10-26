"""K6: `aecom doctor` health report."""

import json
from datetime import timedelta

import pytest

from aecom import cli, doctor
from aecom.bootstrap import seed_demo
from aecom.bus import events
from aecom.bus.events import drain
from aecom.db import AgentRun, Approval, EventRow, now, session
from aecom.secrets import secrets
from aecom.store.adapter import get_store

SKU = "LIC-FOCUSFLOW-PRO-V1"
FAKE_SECRET = "sk-ant-test123"


@pytest.fixture
def demo():
    seed_demo()
    get_store().create_order("c@example.com", SKU, "DE")
    drain()


def by_name(checks):
    return {c["name"]: c for c in checks}


def run_cli(capsys, *args):
    code = 0
    try:
        cli.main(["doctor", *args])
    except SystemExit as e:
        code = e.code or 0
    return code, capsys.readouterr().out


def test_healthy_demo_db_has_no_fail(demo, capsys):
    checks = doctor.run_checks()
    assert {c["name"] for c in checks} == {n for n, _ in doctor.CHECKS}
    assert all(c["status"] in ("ok", "warn", "fail") for c in checks)
    assert [c for c in checks if c["status"] == "fail"] == []
    code, out = run_cli(capsys)
    assert code == 0
    assert "0 fail" in out and "[OK  ] database" in out


def test_dead_letter_fails_and_exits_1(demo, capsys):
    with session() as s:
        s.add(EventRow(topic="order.paid", payload={"order_id": 999}, attempts=5, dead=True, last_error="boom"))
    c = by_name(doctor.run_checks())
    assert c["event_bus"]["status"] == "fail" and "dead letter 1" in c["event_bus"]["detail"]
    assert c["invariants"]["status"] == "fail" and "no_dead_letters" in c["invariants"]["detail"]
    code, out = run_cli(capsys, "--json")
    assert code == 1
    rep = json.loads(out)
    assert rep["summary"]["fail"] >= 2 and rep["version"]


def test_retry_pending_warns():
    with session() as s:
        s.add(EventRow(topic="order.paid", payload={}, attempts=1, next_attempt_at=now() + timedelta(hours=1)))
    assert by_name(doctor.run_checks())["event_bus"]["status"] == "warn"


def test_old_approval_stuck_run_and_kill_switch(capsys):
    from aecom.company_settings import set_setting

    set_setting("kill_switch", "on")
    with session() as s:
        s.add(Approval(id="apr_old", agent_id="finance", tool_name="refund_order", args={}, risk_tier="high", reason="",
                       created_at=now() - timedelta(hours=30)))
        s.add(AgentRun(run_id="r1", agent_id="ceo", model="m", task="t", started_at=now() - timedelta(hours=1)))
    c = by_name(doctor.run_checks())
    assert c["kill_switch"]["status"] == "warn"
    assert c["approvals"]["status"] == "warn" and "1 pending" in c["approvals"]["detail"]
    assert c["stuck_runs"]["status"] == "fail"


def test_missing_key_warns_and_secret_values_never_printed(monkeypatch, capsys, fresh_company):
    fake = {"ANTHROPIC_API_KEY": FAKE_SECRET, "AGENTMAIL_API_KEY": FAKE_SECRET}
    monkeypatch.setattr(secrets, "get", lambda name: fake.get(name))
    monkeypatch.setattr(fresh_company, "llm_mode", "anthropic")
    cred = by_name(doctor.run_checks())["credentials"]
    assert cred["status"] == "warn"
    assert "AECOM_API_TOKEN missing" in cred["detail"] and "ANTHROPIC_API_KEY present" in cred["detail"]
    _, text = run_cli(capsys)
    _, js = run_cli(capsys, "--json")
    assert "credentials" in text and "credentials" in js
    assert FAKE_SECRET not in text and FAKE_SECRET not in js

    fake.pop("ANTHROPIC_API_KEY")
    assert "ANTHROPIC_API_KEY (LLM_MODE=anthropic)" in by_name(doctor.run_checks())["credentials"]["detail"]


def test_check_that_raises_is_reported_as_fail(monkeypatch, capsys):
    def boom():
        raise RuntimeError("bus exploded")

    monkeypatch.setattr(events, "status", boom)
    checks = doctor.run_checks()
    c = by_name(checks)
    assert c["event_bus"] == {"name": "event_bus", "status": "fail",
                              "detail": "check crashed: RuntimeError: bus exploded"}
    assert len(checks) == len(doctor.CHECKS) and c["database"]["status"] == "ok"
    code, out = run_cli(capsys)
    assert code == 1 and "bus exploded" in out


def test_schema_reports_missing_column():
    from sqlalchemy import text

    from aecom.db import get_engine

    with get_engine().begin() as conn:
        conn.execute(text("ALTER TABLE ops_errors DROP COLUMN sentry_event_id"))
    c = by_name(doctor.run_checks())["schema"]
    assert c["status"] == "fail" and "ops_errors.sentry_event_id" in c["detail"]
