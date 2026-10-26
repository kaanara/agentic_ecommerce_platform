"""K2: incident lifecycle - dedup, escalation, auto-resolve, alert repetition, CLI."""

import json
from datetime import datetime, timedelta

import pytest
from aecom import cli, incidents
from aecom.db import AuditLog, session
from sqlalchemy import select

T0 = datetime(2026, 1, 1, 12, 0, 0)
REPEAT = timedelta(minutes=30)


def _audit_decisions() -> list[str]:
    with session() as s:
        return [a.decision for a in s.scalars(select(AuditLog).where(AuditLog.action == "incident").order_by(AuditLog.id))]


def test_dedup_per_monitor_and_subject():
    a, new_a = incidents.raise_incident("undelivered_order", "P2", "ord_1", "stuck", now=T0)
    b, new_b = incidents.raise_incident("undelivered_order", "P2", "ord_1", "still stuck", now=T0 + timedelta(minutes=5))
    c, new_c = incidents.raise_incident("undelivered_order", "P2", "ord_2", now=T0)
    d, new_d = incidents.raise_incident("heartbeat", "P2", "ord_1", now=T0)
    assert (new_a, new_b, new_c, new_d) == (True, False, True, True)
    assert a["id"] == b["id"] and len({a["id"], c["id"], d["id"]}) == 3
    assert b["detail"] == "still stuck" and b["last_seen"] == T0 + timedelta(minutes=5) and b["first_seen"] == T0
    assert len(incidents.list_incidents()) == 3
    assert _audit_decisions() == ["raised", "raised", "raised"]  # a plain refresh is not a state change


def test_dedup_includes_acknowledged():
    a, _ = incidents.raise_incident("heartbeat", "P1", "scheduler", now=T0)
    incidents.acknowledge(a["id"], "kaan")
    b, is_new = incidents.raise_incident("heartbeat", "P1", "scheduler", now=T0 + timedelta(minutes=1))
    assert not is_new and b["id"] == a["id"] and b["status"] == "acknowledged"


def test_severity_escalates_but_never_deescalates():
    a, _ = incidents.raise_incident("ledger_imbalance", "P3", "ledger", now=T0)
    b, _ = incidents.raise_incident("ledger_imbalance", "P1", "ledger", now=T0)
    c, _ = incidents.raise_incident("ledger_imbalance", "P2", "ledger", now=T0)
    assert a["id"] == b["id"] == c["id"]
    assert (a["severity"], b["severity"], c["severity"]) == ("P3", "P1", "P1")
    assert _audit_decisions() == ["raised", "escalated"]
    with pytest.raises(ValueError):
        incidents.raise_incident("ledger_imbalance", "P0", "ledger")


def test_resolve_absent_resolves_only_missing_subjects():
    keep, _ = incidents.raise_incident("undelivered_order", "P2", "ord_1", now=T0)
    gone, _ = incidents.raise_incident("undelivered_order", "P2", "ord_2", now=T0)
    other, _ = incidents.raise_incident("heartbeat", "P1", "ord_2", now=T0)
    resolved = incidents.resolve_absent("undelivered_order", ["ord_1"], now=T0 + timedelta(hours=1))
    assert resolved == [gone["id"]]
    assert incidents.get_incident(gone["id"])["status"] == "resolved"
    assert incidents.get_incident(gone["id"])["resolved_at"] == T0 + timedelta(hours=1)
    assert incidents.get_incident(keep["id"])["status"] == "open"
    assert incidents.get_incident(other["id"])["status"] == "open"  # other monitor untouched
    assert "auto_resolved" in _audit_decisions()
    assert incidents.resolve_absent("undelivered_order", ["ord_1"]) == []  # idempotent


def test_resolved_incident_is_not_reopened():
    a, _ = incidents.raise_incident("heartbeat", "P1", "scheduler", now=T0)
    incidents.resolve(a["id"], "kaan", "restarted")
    b, is_new = incidents.raise_incident("heartbeat", "P1", "scheduler", now=T0 + timedelta(hours=2))
    assert is_new and b["id"] != a["id"] and b["status"] == "open"
    assert incidents.get_incident(a["id"])["status"] == "resolved"
    assert incidents.get_incident(a["id"])["notes"] == "kaan: restarted"


def test_open_counts_and_list_filters():
    p1, _ = incidents.raise_incident("m", "P1", "a", now=T0)
    incidents.raise_incident("m", "P2", "b", now=T0 + timedelta(minutes=1))
    p3, _ = incidents.raise_incident("m", "P3", "c", now=T0 + timedelta(minutes=2))
    incidents.acknowledge(p1["id"], "kaan")
    incidents.resolve(p3["id"], "kaan")
    assert incidents.open_counts() == {"P1": 1, "P2": 1, "P3": 0}
    assert [i["subject"] for i in incidents.list_incidents()] == ["c", "b", "a"]  # newest first
    assert [i["subject"] for i in incidents.list_incidents("acknowledged")] == ["a"]
    assert [i["subject"] for i in incidents.list_incidents(severity="P2")] == ["b"]


def test_needs_alert_p1_repeats_until_acknowledged():
    inc, _ = incidents.raise_incident("heartbeat", "P1", "scheduler", now=T0)
    assert incidents.needs_alert(inc, T0, REPEAT)
    inc = incidents.mark_alerted(inc["id"], now=T0)
    assert inc["alert_count"] == 1
    assert not incidents.needs_alert(inc, T0 + timedelta(minutes=29), REPEAT)
    assert incidents.needs_alert(inc, T0 + timedelta(minutes=30), REPEAT)
    inc = incidents.mark_alerted(inc["id"], now=T0 + timedelta(minutes=30))
    assert inc["alert_count"] == 2
    inc = incidents.acknowledge(inc["id"], "kaan")
    assert not incidents.needs_alert(inc, T0 + timedelta(hours=5), REPEAT)
    assert "alerted" in _audit_decisions() and "acknowledged" in _audit_decisions()


def test_needs_alert_p2_alerts_once():
    inc, _ = incidents.raise_incident("undelivered_order", "P2", "ord_1", now=T0)
    assert incidents.needs_alert(inc, T0, REPEAT)
    inc = incidents.mark_alerted(inc["id"], now=T0)
    assert not incidents.needs_alert(inc, T0 + timedelta(days=1), REPEAT)
    inc = incidents.resolve(inc["id"], "kaan")
    assert not incidents.needs_alert(inc, T0 + timedelta(days=1), REPEAT)


def test_acknowledge_and_resolve_reject_wrong_state():
    inc, _ = incidents.raise_incident("m", "P2", "x", now=T0)
    incidents.resolve(inc["id"], "kaan")
    with pytest.raises(ValueError):
        incidents.acknowledge(inc["id"], "kaan")
    with pytest.raises(ValueError):
        incidents.resolve(inc["id"], "kaan")
    with pytest.raises(ValueError):
        incidents.resolve("inc_missing", "kaan")


def test_cli_list_ack_resolve(capsys):
    a, _ = incidents.raise_incident("heartbeat", "P1", "scheduler", now=T0)
    b, _ = incidents.raise_incident("undelivered_order", "P2", "ord_1", now=T0)
    incidents.resolve(b["id"], "kaan")
    capsys.readouterr()

    cli.main(["incidents"])
    assert [i["id"] for i in json.loads(capsys.readouterr().out)] == [a["id"]]
    cli.main(["incidents", "list", "--all"])
    assert {i["id"] for i in json.loads(capsys.readouterr().out)} == {a["id"], b["id"]}

    cli.main(["incidents", "ack", a["id"], "--by", "kaan", "--note", "looking"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "acknowledged" and out["acknowledged_by"] == "kaan" and out["notes"] == "kaan: looking"

    cli.main(["incidents", "resolve", a["id"], "--by", "kaan", "--note", "fixed"])
    assert json.loads(capsys.readouterr().out)["status"] == "resolved"
    assert incidents.open_counts() == {"P1": 0, "P2": 0, "P3": 0}
    with session() as s:
        actors = {x.actor for x in s.scalars(select(AuditLog).where(AuditLog.action == "incident"))}
    assert "human:kaan" in actors and "system" in actors

    with pytest.raises(SystemExit) as e:
        cli.main(["incidents", "ack", a["id"], "--by", "kaan"])
    assert "not open" in str(e.value.code)
    with pytest.raises(SystemExit) as e:
        cli.main(["incidents", "resolve"])
    assert "incident id required" in str(e.value.code)
