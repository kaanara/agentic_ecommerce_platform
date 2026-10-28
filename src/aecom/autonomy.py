"""Phase 5 autonomy stages: autonomy is earned per action and lost automatically.

A promotable action is a tool whose policy rule defines ``stages:`` (T2 with limits). Its stage (1..3) lives in
``ops_settings["autonomy_stage:<tool>"]``; missing or invalid means 1 (the rule as written). A stage only raises the
per-call ``limits`` / ``price_change_pct_max`` (and may set ``notify``). Hard caps - monthly budgets, recipient rule,
tiers, agent lists, kill switch, pause, safe mode - are never touched. Raising a stage is a human decision with recorded
evidence; lowering is automatic (system) or human and always goes back to stage 1. There is deliberately no agent tool
for any of this.
"""

from __future__ import annotations

from typing import Any

from aecom.audit import audit
from aecom.company_settings import get_setting, set_setting

STAGE_KEY = "autonomy_stage:{tool}"
STAGE_FIELDS = ("limits", "price_change_pct_max", "notify")  # the only keys a stage may override


def _stages(tool: str) -> dict[int, dict[str, Any]]:
    from aecom.policy.gate import load_policy

    raw = (load_policy()["rules"].get(tool) or {}).get("stages") or {}
    out: dict[int, dict[str, Any]] = {}
    for k, v in raw.items():
        try:
            n = int(k)
        except (TypeError, ValueError):
            continue
        if n > 1 and isinstance(v, dict):
            out[n] = v
    return out


def defined_stages(tool: str) -> list[int]:
    """Sorted stages the tool has (always includes 1)."""
    return sorted({1, *_stages(tool)})


def is_promotable(tool: str) -> bool:
    return bool(_stages(tool))


def current_stage(tool: str) -> int:
    """The stage in force: missing / invalid / undefined -> 1; above the highest defined stage -> clamped to it."""
    stages = defined_stages(tool)
    if len(stages) == 1:
        return 1
    try:
        n = int(str(get_setting(STAGE_KEY.format(tool=tool)) or "").strip())
    except ValueError:
        return 1
    if n in stages:
        return n
    return stages[-1] if n > stages[-1] else 1


def effective_rule(tool: str, rule: dict[str, Any], stage: int | None = None) -> dict[str, Any]:
    """``rule`` with the stage's overrides applied (limits merged; only STAGE_FIELDS are taken from a stage)."""
    stage = current_stage(tool) if stage is None else stage
    if stage <= 1:
        return rule
    override = _stages(tool).get(stage) or {}
    merged = dict(rule)
    for key in STAGE_FIELDS:
        if key not in override:
            continue
        if key == "limits":
            merged["limits"] = {**(rule.get("limits") or {}), **(override["limits"] or {})}
        else:
            merged[key] = override[key]
    return merged


def set_stage(tool: str, stage: int, by: str, reason: str = "", *, evidence: dict[str, Any] | None = None) -> int:
    """Set a tool's stage and audit it. An increase needs a named human and non-empty evidence."""
    if not is_promotable(tool):
        raise ValueError(f"{tool} is not a promotable action")
    if stage not in defined_stages(tool):
        raise ValueError(f"{tool} has no stage {stage} (defined: {defined_stages(tool)})")
    old = current_stage(tool)
    if stage > old:
        if not (by or "").strip():
            raise ValueError("raising a stage needs the name of the human (by)")
        if not evidence:
            raise ValueError("raising a stage needs recorded evidence")
    set_setting(STAGE_KEY.format(tool=tool), str(stage))
    actor = "system" if by == "system" or not by else f"human:{by}"
    audit(actor, "autonomy_stage", "promoted" if stage > old else "demoted",
          {"tool": tool, "from": old, "to": stage, "reason": reason, "evidence": evidence})
    return stage


def demote(tool: str, reason: str, by: str = "system") -> int:
    """Back to stage 1. No-op (no audit) when already there. Returns the stage after the call (1)."""
    if current_stage(tool) == 1:
        return 1
    return set_stage(tool, 1, by, reason)


def promoted_tools() -> dict[str, int]:
    """{tool: stage} for every promotable tool above stage 1."""
    from aecom.policy.gate import load_policy

    return {tool: n for tool in load_policy()["rules"] if is_promotable(tool) and (n := current_stage(tool)) > 1}
