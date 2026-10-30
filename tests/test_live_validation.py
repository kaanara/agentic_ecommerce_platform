"""Live validation (data/live_validation report, real-API gate, throwaway DB, spend cap) — no network/LLM calls.

Every test here stubs ``aecom.live_validation.run_scenario`` with a fake coroutine that writes its own ``AgentRun``
rows (via the currently configured, throwaway database) instead of running real agents — ``LLM_MODE=anthropic`` is
set so the real-API gate is satisfied, but nothing ever reaches the network.
"""

from __future__ import annotations

import json
import shutil

import pytest
from sqlalchemy import select

from aecom import live_validation
from aecom.config import get_settings
from aecom.db import AgentRun, new_id, session


@pytest.fixture(autouse=True)
def _cleanup_report_dir():
    """``live_validation.REPORT_ROOT`` is a real repo path (data/live_validation/<timestamp>/), not tmp_path —
    required by the task spec. Remove whatever this test created so the worktree stays clean."""
    yield
    shutil.rmtree(live_validation.REPORT_ROOT, ignore_errors=True)


def _enable_anthropic(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "anthropic")
    monkeypatch.setattr(live_validation.secrets, "get", lambda name: "sk-ant-fake00000000000000" if name == "ANTHROPIC_API_KEY" else None)
    return settings


def _fake_scenarios():
    return {"fake-1": object(), "fake-2": object()}


def _make_fake_run_scenario(*, tokens=(10_000, 2_000), tier="sonnet", agent_id="finance",
                            verdict_by_name=None, approvals_by_name=None, raise_for=None):
    """Builds a fake ``run_scenario(name, offline=...)`` coroutine: inserts one AgentRun row (cost computed the
    same way the real agent loop does, via ``settings.cost_eur``) on whichever database is currently configured,
    and returns a result shaped like the real ``run_scenario`` (scenario/ctx/result/pending_approvals)."""
    verdict_by_name = verdict_by_name or {}
    approvals_by_name = approvals_by_name or {}
    raise_for = raise_for or {}

    async def fake_run_scenario(name, offline=False):
        if name in raise_for:
            raise RuntimeError(raise_for[name])
        settings = get_settings()
        tin, tout = tokens
        cost = settings.cost_eur(tier, tin, tout)
        run_id = new_id("run")
        with session() as s:
            s.add(AgentRun(run_id=run_id, agent_id=agent_id, model=settings.model_for_tier(tier), task="fake task",
                           output="ok", status="ok", steps=1, tool_calls=0, input_tokens=tin, output_tokens=tout,
                           cost_eur=cost))
        return {"scenario": name, "ctx": {"run_id": run_id}, "result": verdict_by_name.get(name, {}),
               "pending_approvals": approvals_by_name.get(name, []), "last_email": None}

    return fake_run_scenario


# ------------------------------------------------------------------------------------------------- refusals
def test_refuses_without_anthropic_llm_mode():
    assert get_settings().llm_mode == "scripted"  # the autouse fixture's default
    with pytest.raises(live_validation.LiveValidationRefused, match="LLM_MODE"):
        live_validation.run()


def test_refuses_without_api_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "anthropic")
    monkeypatch.setattr(live_validation.secrets, "get", lambda name: None)
    with pytest.raises(live_validation.LiveValidationRefused, match="ANTHROPIC_API_KEY"):
        live_validation.run()


def test_refuses_unknown_scenario(monkeypatch):
    _enable_anthropic(monkeypatch)
    with pytest.raises(live_validation.LiveValidationRefused, match="unknown scenario"):
        live_validation.run(names=["not-a-real-scenario"])


# ------------------------------------------------------------------------------------------------- happy path
def test_runs_fake_scenarios_on_throwaway_db_and_restores_settings(monkeypatch):
    settings = _enable_anthropic(monkeypatch)
    saved_url = settings.database_url
    monkeypatch.setattr(live_validation, "SCENARIOS", _fake_scenarios())
    monkeypatch.setattr(live_validation, "run_scenario", _make_fake_run_scenario(
        verdict_by_name={"fake-1": {"verdict": {"approved": True, "score": 8.0}}},
        approvals_by_name={"fake-1": [{"id": "appr_1"}]}))
    orig_daily_budget = live_validation_agents_mod().daily_budget

    report = live_validation.run(max_eur=2.0, by="tester")

    assert get_settings().database_url == saved_url  # restored, never left pointing at the throwaway DB
    assert live_validation_agents_mod().daily_budget is orig_daily_budget  # patch cleanly removed afterwards
    assert [sc["name"] for sc in report["scenarios"]] == ["fake-1", "fake-2"]
    assert all(sc["status"] == "completed" for sc in report["scenarios"])
    assert all(sc["agent_runs"] == 1 for sc in report["scenarios"])
    first = report["scenarios"][0]
    assert first["approvals_created"] == 1
    assert first["verdict"] == {"approved": True, "score": 8.0}
    assert first["verdict_sane"] is True
    second = report["scenarios"][1]
    assert second["verdict"] is None and second["verdict_sane"] is None
    assert report["total_cost_eur"] == pytest.approx(sum(sc["cost_eur"] for sc in report["scenarios"]))


def live_validation_agents_mod():
    from aecom.graph import agents

    return agents


def test_writes_json_and_markdown_report(monkeypatch):
    _enable_anthropic(monkeypatch)
    monkeypatch.setattr(live_validation, "SCENARIOS", _fake_scenarios())
    monkeypatch.setattr(live_validation, "run_scenario", _make_fake_run_scenario())

    report = live_validation.run(max_eur=2.0, by="tester")

    out_dir = live_validation.REPORT_ROOT / report["started_at"]
    json_path, md_path = out_dir / "report.json", out_dir / "report.md"
    assert json_path.is_file() and md_path.is_file()
    on_disk = json.loads(json_path.read_text())
    assert on_disk["by"] == "tester"
    assert [sc["name"] for sc in on_disk["scenarios"]] == ["fake-1", "fake-2"]
    md = md_path.read_text()
    assert "fake-1" in md and "fake-2" in md and "Price calibration" in md


# ------------------------------------------------------------------------------------------------- spend cap
def test_honours_cap_and_stops_before_next_scenario(monkeypatch):
    _enable_anthropic(monkeypatch)
    monkeypatch.setattr(live_validation, "SCENARIOS", _fake_scenarios())
    monkeypatch.setattr(live_validation, "run_scenario", _make_fake_run_scenario(tokens=(10_000, 2_000)))
    # default price_sonnet/usd_to_eur -> one fake scenario costs ~0.0552 EUR; cap just under that.
    report = live_validation.run(max_eur=0.05, by="tester")

    first, second = report["scenarios"]
    assert first["status"] == "completed" and first["cost_eur"] > 0.05
    assert second["status"] == "skipped"
    assert "cap" in second["reason"]
    assert report["total_cost_eur"] == pytest.approx(first["cost_eur"])


# ------------------------------------------------------------------------------------------------- error masking
def test_masks_errors(monkeypatch):
    _enable_anthropic(monkeypatch)
    monkeypatch.setattr(live_validation, "SCENARIOS", _fake_scenarios())
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
    monkeypatch.setattr(live_validation, "run_scenario", _make_fake_run_scenario(raise_for={"fake-1": f"boom {secret}"}))

    report = live_validation.run(max_eur=2.0, by="tester")

    first = report["scenarios"][0]
    assert first["status"] == "error"
    assert secret not in first["reason"]
    assert "***" in first["reason"]
    assert secret not in json.dumps(report)


# ------------------------------------------------------------------------------------------------- calibration
def test_calibration_flags_price_drift_and_suggests_reference_price(monkeypatch):
    settings = _enable_anthropic(monkeypatch)
    monkeypatch.setattr(settings, "price_sonnet", (6.0, 30.0))  # 2x aecom.live_validation.REFERENCE_PRICE_USD["sonnet"]
    monkeypatch.setattr(live_validation, "SCENARIOS", _fake_scenarios())
    monkeypatch.setattr(live_validation, "run_scenario", _make_fake_run_scenario(tokens=(10_000, 2_000), tier="sonnet"))

    report = live_validation.run(max_eur=2.0, by="tester")

    [calib] = report["calibration"]
    assert calib["tier"] == "sonnet" and calib["runs"] == 2
    assert calib["flagged"] is True
    assert abs(calib["gap_pct"]) > 15
    assert calib["suggested_price_usd_per_m"] == list(live_validation.REFERENCE_PRICE_USD["sonnet"])


def test_calibration_empty_when_no_tokens_recorded(monkeypatch):
    _enable_anthropic(monkeypatch)
    monkeypatch.setattr(live_validation, "SCENARIOS", _fake_scenarios())

    async def fake_no_tokens(name, offline=False):
        return {"scenario": name, "ctx": {}, "result": {}, "pending_approvals": [], "last_email": None}

    monkeypatch.setattr(live_validation, "run_scenario", fake_no_tokens)
    report = live_validation.run(max_eur=2.0, by="tester")
    assert report["calibration"] == []
    assert all(sc["agent_runs"] == 0 for sc in report["scenarios"])


# ------------------------------------------------------------------------------------------------- isolation (G8-style)
def test_scenario_agent_runs_are_isolated_per_scenario(monkeypatch):
    """Each scenario gets its own throwaway DB file (mirrors ``aecom.chaos``), so one scenario's AgentRun rows
    never leak into the next scenario's count."""
    _enable_anthropic(monkeypatch)
    monkeypatch.setattr(live_validation, "SCENARIOS", _fake_scenarios())
    monkeypatch.setattr(live_validation, "run_scenario", _make_fake_run_scenario())

    report = live_validation.run(max_eur=2.0, by="tester")
    assert [sc["agent_runs"] for sc in report["scenarios"]] == [1, 1]

    # the configured (test fixture) database is untouched: no stray AgentRun rows from the live-validation run
    with session() as s:
        assert s.scalars(select(AgentRun)).all() == []
