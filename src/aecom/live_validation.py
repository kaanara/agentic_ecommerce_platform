"""Roadmap "Next" item 1: run every scenario against the real Claude API, record outcomes and costs, and flag
``PRICE_*`` drift.

``run()`` NEVER touches the configured/company database: it creates its own throwaway SQLite file per scenario
under the system temp directory (the same technique ``aecom.chaos`` and ``aecom chaos`` use, see
``aecom.chaos._is_disposable_db``), switches ``settings.database_url`` to it, resets the engine, and restores the
saved URL and engine afterwards even if a scenario crashes.

It refuses outright unless ``LLM_MODE=anthropic`` and ``ANTHROPIC_API_KEY`` is set — this module exists to spend
real money against the real API, so it must never silently replay scripted agents, and it must never run without a
key that is actually usable.

A hard total spend cap (``max_eur``) is enforced two ways: between scenarios (checked before starting the next
one) and, as a best-effort cushion within a scenario, by capping every agent's daily budget to what is left of the
cap for the remainder of the run (``aecom.graph.agents.daily_budget`` is wrapped for the duration of ``run()``) so
a single runaway scenario cannot blow through the cap by much before the between-scenario check catches it.

The report (per-scenario status/cost/tokens/approvals, plus a price-calibration table) is written as JSON and
Markdown under ``data/live_validation/<timestamp>/`` — never under ``BACKUP_DIR`` — with every string run through
``aecom.observability.mask`` first. ``.env`` is never written to; calibration only *suggests* ``PRICE_*`` values.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections import defaultdict
from typing import Any
from unittest.mock import patch

from sqlalchemy import select

from aecom.config import ROOT, Settings, get_settings
from aecom.db import AgentRun, now, session
from aecom.observability import mask
from aecom.scenarios import SCENARIOS, run_scenario
from aecom.secrets import secrets

REPORT_ROOT = ROOT / "data" / "live_validation"

# Anthropic's published USD list price per 1M tokens (input, output), by model tier. This is the ground truth the
# calibration table compares the configured ``price_opus``/``price_sonnet``/``price_haiku`` settings against; update
# it here whenever Anthropic changes its published pricing so drift detection stays meaningful.
REFERENCE_PRICE_USD: dict[str, tuple[float, float]] = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
CALIBRATION_GAP_THRESHOLD = 0.15  # flag a >15% gap between configured and reference price


class LiveValidationRefused(RuntimeError):
    """Raised by ``run`` when real-API preconditions (LLM_MODE / ANTHROPIC_API_KEY) are not met."""


def _tier_for_model(model: str, settings: Settings) -> str | None:
    by_model = {settings.model_for_tier(t): t for t in ("opus", "sonnet", "haiku")}
    return by_model.get(model)


def _agent_run_rows() -> list[dict[str, Any]]:
    """Plain dicts (not ORM rows) for every ``AgentRun`` on the currently configured database."""
    with session() as s:
        rows = s.scalars(select(AgentRun)).all()
        return [{"agent_id": r.agent_id, "model": r.model, "input_tokens": r.input_tokens,
                 "output_tokens": r.output_tokens, "cost_eur": r.cost_eur} for r in rows]


def _check_verdict(result: Any) -> tuple[dict[str, Any] | None, bool | None]:
    """If a scenario's result carries a Judge ``verdict`` (company/selection graph state), sanity-check its shape:
    ``approved`` must be a bool and, if present, ``score`` must be a number in [0, 10]. Scenarios without a verdict
    (e.g. order-delivery, support-refund) return ``(None, None)``: the check does not apply."""
    if not isinstance(result, dict) or "verdict" not in result:
        return None, None
    verdict = result["verdict"]
    if not isinstance(verdict, dict):
        return verdict, False
    approved = verdict.get("approved")
    score = verdict.get("score")
    sane = isinstance(approved, bool) and (score is None or (isinstance(score, (int, float)) and 0 <= score <= 10))
    return verdict, sane


async def _run_one(name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    t0 = time.monotonic()
    try:
        result = await run_scenario(name, offline=False)
    except Exception as exc:  # scenario crashed outright (not just one agent step returning status=error)
        wall = time.monotonic() - t0
        return {"name": name, "status": "error", "reason": mask(f"{type(exc).__name__}: {exc}"), "wall_s": wall,
                "agent_runs": 0, "tokens_in": 0, "tokens_out": 0, "cost_eur": 0.0, "approvals_created": 0,
                "verdict": None, "verdict_sane": None}, []
    wall = time.monotonic() - t0
    runs = _agent_run_rows()
    tokens_in = sum(r["input_tokens"] for r in runs)
    tokens_out = sum(r["output_tokens"] for r in runs)
    cost = sum(r["cost_eur"] for r in runs)
    approvals = len(result.get("pending_approvals") or [])
    verdict, verdict_sane = _check_verdict(result.get("result"))
    return {"name": name, "status": "completed", "reason": None, "wall_s": wall, "agent_runs": len(runs),
            "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_eur": cost, "approvals_created": approvals,
            "verdict": verdict, "verdict_sane": verdict_sane}, runs


def _calibration_table(agent_runs: list[dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    by_tier: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in agent_runs:
        if not (r["input_tokens"] or r["output_tokens"]):
            continue
        tier = _tier_for_model(r["model"], settings)
        if tier:
            by_tier[tier].append(r)
    configured = {"opus": settings.price_opus, "sonnet": settings.price_sonnet, "haiku": settings.price_haiku}
    table = []
    for tier, runs in sorted(by_tier.items()):
        observed_avg = sum(r["cost_eur"] for r in runs) / len(runs)
        ref_in, ref_out = REFERENCE_PRICE_USD.get(tier, (0.0, 0.0))
        predicted_costs = [(r["input_tokens"] * ref_in + r["output_tokens"] * ref_out) / 1_000_000 * settings.usd_to_eur
                            for r in runs]
        predicted_avg = sum(predicted_costs) / len(predicted_costs)
        gap = None if predicted_avg == 0 else (observed_avg - predicted_avg) / predicted_avg
        flagged = gap is not None and abs(gap) > CALIBRATION_GAP_THRESHOLD
        table.append({
            "tier": tier, "runs": len(runs),
            "observed_avg_cost_eur": round(observed_avg, 6), "predicted_avg_cost_eur": round(predicted_avg, 6),
            "gap_pct": None if gap is None else round(gap * 100, 1), "flagged": flagged,
            "configured_price_usd_per_m": list(configured[tier]), "reference_price_usd_per_m": [ref_in, ref_out],
            "suggested_price_usd_per_m": [ref_in, ref_out] if flagged else None,
        })
    return table


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Live validation — {report['started_at']}", "",
             f"By **{report['by']}**, cap €{report['max_eur']:.2f}, spent €{report['total_cost_eur']:.4f}, "
             f"wall time {report['total_wall_s']:.1f}s.", "",
             "## Scenarios", "",
             "| scenario | status | wall (s) | agent runs | tokens in/out | cost (EUR) | approvals | verdict |",
             "|---|---|---|---|---|---|---|---|"]
    for sc in report["scenarios"]:
        verdict = "-" if sc["verdict"] is None else ("ok" if sc["verdict_sane"] else "SUSPECT")
        lines.append(f"| {sc['name']} | {sc['status']} | {sc['wall_s']:.1f} | {sc['agent_runs']} | "
                     f"{sc['tokens_in']}/{sc['tokens_out']} | {sc['cost_eur']:.4f} | {sc['approvals_created']} | "
                     f"{verdict} |")
        if sc["reason"]:
            lines.append(f"  - {sc['reason']}")
    lines += ["", "## Price calibration", "",
              "| tier | runs | observed €/run | predicted €/run (reference price) | gap | flagged | suggested USD/M |",
              "|---|---|---|---|---|---|---|"]
    if not report["calibration"]:
        lines.append("| - | - | - | - | - | - | no token-bearing AgentRun rows |")
    for c in report["calibration"]:
        gap = "-" if c["gap_pct"] is None else f"{c['gap_pct']}%"
        suggested = "-" if not c["suggested_price_usd_per_m"] else f"{c['suggested_price_usd_per_m']}"
        lines.append(f"| {c['tier']} | {c['runs']} | {c['observed_avg_cost_eur']:.6f} | "
                     f"{c['predicted_avg_cost_eur']:.6f} | {gap} | {'yes' if c['flagged'] else 'no'} | {suggested} |")
    return "\n".join(lines) + "\n"


def _write_report(report: dict[str, Any]) -> tuple[Any, Any]:
    out_dir = REPORT_ROOT / report["started_at"]
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "report.json", out_dir / "report.md"
    json_path.write_text(mask(json.dumps(report, indent=2, default=str)))
    md_path.write_text(mask(_render_markdown(report)))
    return json_path, md_path


def run(names: list[str] | None = None, max_eur: float = 2.0, by: str = "cli-user") -> dict[str, Any]:
    """Run ``names`` (default: every scenario) against the real Claude API, capped at ``max_eur`` total spend.

    Refuses (``LiveValidationRefused``) unless ``LLM_MODE=anthropic`` and ``ANTHROPIC_API_KEY`` is set. Always runs
    on a fresh throwaway SQLite database per scenario (never the configured/company database); the configured
    database URL and engine are restored before returning, even on error. Writes JSON + Markdown reports under
    ``data/live_validation/<timestamp>/`` and returns the same report dict.
    """
    settings = get_settings()
    if settings.llm_mode != "anthropic":
        raise LiveValidationRefused(
            f"live validation refused: LLM_MODE is {settings.llm_mode!r}, not 'anthropic' — scripted mode never "
            "calls the real API, so there is nothing to validate or cost. Set LLM_MODE=anthropic and re-run.")
    if not secrets.get("ANTHROPIC_API_KEY"):
        raise LiveValidationRefused(
            "live validation refused: ANTHROPIC_API_KEY is not set (.env or environment) — a capped key is "
            "required to run real scenarios.")

    chosen = list(SCENARIOS) if not names else list(names)
    unknown = [n for n in chosen if n not in SCENARIOS]
    if unknown:
        raise LiveValidationRefused(f"unknown scenario(s): {', '.join(unknown)}; choose from {', '.join(SCENARIOS)}")

    from aecom import db
    from aecom.bootstrap import init
    from aecom.graph import agents as agents_mod

    started_at = now().strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {"started_at": started_at, "by": by, "max_eur": max_eur, "scenarios": [],
                              "calibration": [], "total_cost_eur": 0.0, "total_wall_s": 0.0}
    spent, all_runs = 0.0, []
    saved_url = settings.database_url
    orig_daily_budget = agents_mod.daily_budget

    def capped_daily_budget(agent_id: str) -> float:
        # live, not just between scenarios: re-reads the current (per-scenario, throwaway-DB) AgentRun cost each
        # time an agent is about to run, so the per-agent budget shrinks as the scenario itself spends.
        scenario_spent = sum(r["cost_eur"] for r in _agent_run_rows())
        return min(orig_daily_budget(agent_id), max(0.0, max_eur - spent - scenario_spent))

    with tempfile.TemporaryDirectory(prefix="aecom-live-validation-") as tmp:
        try:
            with patch.object(agents_mod, "daily_budget", capped_daily_budget):
                for scenario_name in chosen:
                    if spent >= max_eur:
                        report["scenarios"].append({
                            "name": scenario_name, "status": "skipped",
                            "reason": f"spend cap reached (€{spent:.4f} >= €{max_eur:.2f}) before this scenario",
                            "wall_s": 0.0, "agent_runs": 0, "tokens_in": 0, "tokens_out": 0, "cost_eur": 0.0,
                            "approvals_created": 0, "verdict": None, "verdict_sane": None})
                        continue
                    db.reset_engine(f"sqlite:///{tmp}/{scenario_name}.db")
                    init()
                    entry, runs = _run_scenario_sync(scenario_name)
                    spent += entry["cost_eur"]
                    all_runs.extend(runs)
                    report["scenarios"].append(entry)
        finally:
            if db._engine is not None:
                db._engine.dispose()
            settings.database_url = saved_url
            db.reset_engine()

    report["total_cost_eur"] = spent
    report["total_wall_s"] = sum(sc["wall_s"] for sc in report["scenarios"])
    report["calibration"] = _calibration_table(all_runs, settings)
    report = json.loads(mask(json.dumps(report, default=str)))  # belt-and-braces: mask secret-looking strings
    json_path, md_path = _write_report(report)
    print(_render_markdown(report))
    print(f"report written to {json_path} and {md_path}")
    return report


def _run_scenario_sync(name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Sync wrapper: ``run()`` is a plain synchronous entry point (CLI-friendly), scenarios are async."""
    return asyncio.run(_run_one(name))
