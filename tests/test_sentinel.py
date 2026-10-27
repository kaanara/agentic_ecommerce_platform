"""K4/K3: the Sentinel (read-only incident diagnosis), its diagnostic tools and the runbook skills."""

import asyncio
import json
import re

import pytest
from aecom import alerts, incidents, monitors, sentinel
from aecom.bootstrap import RUNBOOKS_FILE, parse_runbooks, seed_runbook_skills
from aecom.company_settings import set_agent_enabled
from aecom.db import AgentRun, Allowlist, AuditLog, ErrorRow, RegistryTool, session
from aecom.graph import agents
from aecom.llm import Script
from aecom.persona.loader import load_spec
from aecom.policy.gate import evaluate, gate_builtin
from aecom.registry.tools import allowed_tools, seed_registry, skills_for
from aecom.scenarios import tc
from aecom.tools.catalog import CATALOG
from sqlalchemy import func, select

DIAGNOSTICS = ["list_open_incidents", "get_incident", "recent_errors", "run_timeline", "event_bus_status",
               "dead_letters", "doctor_report"]
ORDER = "ord_sentinel1"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No alerts or heartbeat over the network; one fake P1 monitor; no leftover sentinel script afterwards."""
    monkeypatch.setattr(alerts, "route", lambda inc: None)
    monkeypatch.setattr(alerts, "heartbeat", lambda: "skipped")
    failing = {"on": True}
    monkeypatch.setattr(monitors, "MONITORS", {"undelivered_order": (
        "P1", lambda now: [(ORDER, f"order {ORDER} is 'paid' for over 15 min")] if failing["on"] else [])})
    yield failing
    Script.queues.clear()


def _incident_id(messages) -> str:
    return re.search(r"Diagnose incident (inc_\w+)", str(messages[1].content)).group(1)


def diagnosis_script() -> list:
    return [tc("list_open_incidents"),
            lambda msgs: tc("submit_diagnosis", incident_id=_incident_id(msgs),
                            summary="Order paid but not delivered.",
                            root_cause="1) licence pool empty (high) 2) kill switch on (low)",
                            evidence=["get_incident: order is 'paid' for over 15 min"],
                            next_actions=["uv run aecom reconcile"])]


def sentinel_runs() -> int:
    with session() as s:
        return s.scalar(select(func.count(AgentRun.id)).where(AgentRun.agent_id == "sentinel")) or 0


def the_incident() -> dict:
    (inc,) = incidents.list_incidents(status="open")
    return inc


# ----------------------------------------------------------------------------- persona, tools, policy
def test_persona_is_sonnet_with_only_read_only_tools_and_submit_diagnosis():
    spec = load_spec("sentinel")
    assert (spec.model_tier, spec.role, spec.reports_to) == ("sonnet", "Operations Sentinel", "ceo")
    assert spec.identity["layer"] == "evaluation"
    assert set(DIAGNOSTICS) | {"submit_diagnosis"} <= set(spec.tools)
    side_effecting = {t for t in spec.tools if CATALOG[t].side_effect}
    assert side_effecting == set()  # strictly read-only: account needs go into the diagnosis' next actions
    assert [t for t in spec.tools if CATALOG[t].terminal] == ["submit_diagnosis"]
    assert spec.budget_eur_per_day == 3


def test_diagnostic_tools_are_in_catalog_registry_and_policy():
    for name in DIAGNOSTICS + ["submit_diagnosis"]:
        assert name in CATALOG and CATALOG[name].domain == "ops"
        assert evaluate("sentinel", name, {}, side_effect=CATALOG[name].side_effect).decision == "allow"
    for name in DIAGNOSTICS:  # read-only: not restricted
        assert evaluate("support", name, {}, side_effect=False).tier == "T0"
        assert evaluate("support", name, {}, side_effect=False).decision == "allow"
    restricted = evaluate("support", "submit_diagnosis", {}, side_effect=False)
    assert (restricted.decision, restricted.tier) == ("deny", "T1")
    seed_registry()
    with session() as s:
        assert all(s.get(RegistryTool, n) is not None for n in DIAGNOSTICS + ["submit_diagnosis"])
    assert set(DIAGNOSTICS) | {"submit_diagnosis"} <= set(allowed_tools("sentinel"))


def test_sentinel_cannot_call_side_effecting_tools():
    assert evaluate("sentinel", "refund_order", {"amount_eur": 5}).decision == "deny"
    assert evaluate("sentinel", "deliver_order", {"order_id": "x"}).decision == "deny"
    assert evaluate("sentinel", "pause_campaign", {"campaign_id": "x"}).decision == "deny"
    out = asyncio.run(gate_builtin("refund_order", "sentinel").ainvoke(
        {"order_id": "x", "amount_eur": 5, "reason": "test"}))
    assert out.startswith("DENIED")
    assert "refund_order" not in allowed_tools("sentinel")  # and it is never handed the tool in a run


def test_diagnostic_tools_return_masked_short_data():
    from aecom.observability import record_error

    try:
        def deep(n):
            if n == 0:
                raise RuntimeError("boom")
            deep(n - 1)
        deep(30)
    except RuntimeError as exc:
        record_error("gate", exc, context={"x": 1})
    (err,) = CATALOG["recent_errors"].fn(component="gate")
    assert err["exc_type"] == "RuntimeError" and err["traceback"].startswith("…")
    assert len(err["traceback"].splitlines()) <= 13
    assert CATALOG["recent_errors"].fn(component="agent") == []
    assert set(CATALOG["event_bus_status"].fn()) == {"waiting_for_retry", "queued", "dead_letter"}
    assert {c["name"] for c in CATALOG["doctor_report"].fn()} >= {"database", "event_bus"}
    assert CATALOG["run_timeline"].fn("run_missing")["timeline"] == []


# ----------------------------------------------------------------------------- K3 runbooks as skills
def test_runbook_skills_seeded_once_and_granted_to_sentinel():
    expected = parse_runbooks(RUNBOOKS_FILE.read_text())
    assert {"runbook_undelivered_order", "runbook_monitor_error", "runbook_always_first"} <= set(expected)
    assert "## undelivered_order (P1)" in expected["runbook_undelivered_order"]
    with session() as s:  # init() (fixture) already seeded them
        rows = list(s.scalars(select(RegistryTool).where(RegistryTool.name.like("runbook_%"))))
        assert {r.name for r in rows} == set(expected)
        assert all(r.kind == "skill" and r.status == "approved" for r in rows)
    assert seed_runbook_skills() == []  # idempotent
    with session() as s:
        grants = s.scalar(select(func.count(Allowlist.id)).where(Allowlist.agent_id == "sentinel",
                                                                 Allowlist.tool_name.like("runbook_%")))
        assert grants == len(expected)
        assert s.scalar(select(func.count(Allowlist.id)).where(Allowlist.agent_id == "support",
                                                               Allowlist.tool_name.like("runbook_%"))) == 0
    prompt = "\n".join(skills_for("sentinel"))
    assert "## Skill: runbook_undelivered_order" in prompt and "Safe actions" in prompt


def test_runbook_seeding_skips_missing_file(tmp_path):
    assert seed_runbook_skills(tmp_path / "nope.md") == []


# ----------------------------------------------------------------------------- K4 trigger from the monitors
def test_new_p1_triggers_exactly_one_diagnosis_in_the_notes():
    Script.load({"sentinel": diagnosis_script()})
    r = monitors.run_monitors()
    inc = the_incident()
    assert r["diagnosed"] == [inc["id"]]
    assert sentinel_runs() == 1
    assert "sentinel: DIAGNOSIS" in inc["notes"] and "licence pool empty" in inc["notes"]
    assert "- uv run aecom reconcile" in inc["notes"]
    assert inc["status"] == "open"  # the Sentinel never changes the incident itself
    with session() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "incident", AuditLog.decision == "diagnosed")).one()
        run = s.scalars(select(AgentRun).where(AgentRun.agent_id == "sentinel")).one()
        assert run.status == "ok" and json.loads(run.output)["recorded"] is True

    Script.load({"sentinel": diagnosis_script()})  # a script is available again, but the incident is not new
    assert monitors.run_monitors()["diagnosed"] == []
    assert sentinel.diagnose(inc["id"]) is None  # at most once, even when asked directly
    assert sentinel_runs() == 1
    assert the_incident()["notes"].count("DIAGNOSIS") == 1


def test_no_diagnosis_without_script_when_disabled_or_switched_off(monkeypatch):
    monitors.run_monitors()  # scripted mode without a sentinel script: skipped, not a "nothing to do" run
    assert sentinel_runs() == 0
    inc = the_incident()
    assert not sentinel.already_diagnosed(inc["id"])

    Script.load({"sentinel": diagnosis_script()})
    set_agent_enabled("sentinel", False)
    assert sentinel.available() is False
    assert monitors.diagnose_new([inc]) == []
    set_agent_enabled("sentinel", True)
    monkeypatch.setattr(monitors, "AUTO_DIAGNOSE", False)
    assert monitors.diagnose_new([inc]) == []
    assert sentinel_runs() == 0


def test_p3_incidents_are_not_diagnosed():
    Script.load({"sentinel": diagnosis_script()})
    inc, _ = incidents.raise_incident("custom", "P3", "x", "minor")
    assert monitors.diagnose_new([inc]) == []
    assert sentinel_runs() == 0


def test_failing_sentinel_run_never_escapes_run_monitors(monkeypatch):
    Script.load({"sentinel": diagnosis_script()})

    async def boom(*_a, **_k):
        raise RuntimeError("sentinel exploded")

    monkeypatch.setattr(agents, "run_agent", boom)
    r = monitors.run_monitors()
    assert r["diagnosed"] == [] and r["heartbeat"] == "skipped"
    with session() as s:
        assert s.scalars(select(ErrorRow).where(ErrorRow.component == "sentinel")).one().exc_type == "RuntimeError"
    assert sentinel.already_diagnosed(the_incident()["id"])  # not retried every 5 minutes

    def worse(_incident_id):
        raise RuntimeError("trigger exploded")

    monkeypatch.setattr(sentinel, "diagnose", worse)
    inc, _ = incidents.raise_incident("custom", "P1", "y", "other")
    assert monitors.diagnose_new([inc]) == []
    with session() as s:
        assert s.scalars(select(ErrorRow).where(ErrorRow.component == "monitors")).one().context["step"] == "sentinel"


def test_submit_diagnosis_rejects_unknown_incident():
    with pytest.raises(ValueError):
        sentinel.append_diagnosis("inc_missing", "s", "r", [], [])
