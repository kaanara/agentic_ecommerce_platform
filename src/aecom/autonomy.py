"""Phase 5 autonomy stages: autonomy is earned per action and lost automatically.

A promotable action is a tool whose policy rule defines ``stages:`` (T2 with limits). Its stage (1..3) lives in
``ops_settings["autonomy_stage:<tool>"]``; missing or invalid means 1 (the rule as written). A stage only raises the
per-call ``limits`` / ``price_change_pct_max`` (and may set ``notify``). Hard caps - monthly budgets, recipient rule,
tiers, agent lists, kill switch, pause, safe mode - are never touched. Raising a stage is a human decision with recorded
evidence; lowering is automatic (system) or human and always goes back to stage 1. There is deliberately no agent tool
for any of this.

R19: ``set_stage`` reads the current stage, validates and writes the new one, and audits the change in a single DB
transaction. When a caller (``trust.promote``) passes ``expected_old_stage``, that read is also compared against it
as a guard: if something else (another promotion, or an automatic demotion) already moved the stage, the write is
refused with ``StageConflict`` instead of silently overwriting - see ``trust.promote`` and docs/wiki/Autonomy.md.
"""

from __future__ import annotations

from typing import Any

from aecom.audit import mask_detail
from aecom.company_settings import get_setting
from aecom.db import AuditLog, Setting, session

STAGE_KEY = "autonomy_stage:{tool}"
STAGE_FIELDS = ("limits", "price_change_pct_max", "notify")  # the only keys a stage may override


class StageConflict(ValueError):
    """``set_stage`` was given an ``expected_old_stage`` that no longer matches the database: another promotion or
    an automatic demotion raced in between. The caller should re-check eligibility and retry, not assume it won."""


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


def _decode_stage(stages: list[int], raw: str | None) -> int:
    """Turn a raw ``ops_settings`` value into a valid stage: missing / invalid -> 1, above the highest -> clamped."""
    try:
        n = int(str(raw or "").strip())
    except ValueError:
        return 1
    if n in stages:
        return n
    return stages[-1] if n > stages[-1] else 1


def current_stage(tool: str) -> int:
    """The stage in force: missing / invalid / undefined -> 1; above the highest defined stage -> clamped to it."""
    stages = defined_stages(tool)
    if len(stages) == 1:
        return 1
    return _decode_stage(stages, get_setting(STAGE_KEY.format(tool=tool)))


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


def set_stage(tool: str, stage: int, by: str, reason: str = "", *, evidence: dict[str, Any] | None = None,
              expected_old_stage: int | None = None) -> int:
    """Set a tool's stage and audit it, as one atomic transaction. An increase needs a named human and non-empty
    evidence. If ``expected_old_stage`` is given, the write only proceeds when the stage on disk still matches it
    (read inside the same transaction) - otherwise ``StageConflict`` is raised (R19: closes the gap between an
    eligibility check and the write it leads to). Without it, the fresh in-transaction read is still used, so a
    racing call lands on a consistent value instead of mislabeling a no-op as a demotion."""
    if not is_promotable(tool):
        raise ValueError(f"{tool} is not a promotable action")
    stages = defined_stages(tool)
    if stage not in stages:
        raise ValueError(f"{tool} has no stage {stage} (defined: {stages})")
    key = STAGE_KEY.format(tool=tool)
    with session() as s:
        row = s.get(Setting, key)
        old = _decode_stage(stages, row.value if row else None)
        if expected_old_stage is not None and old != expected_old_stage:
            raise StageConflict(f"{tool} changed stage concurrently (expected {expected_old_stage}, now {old}); "
                                 "re-check eligibility and retry")
        if stage == old:
            return stage  # nothing to do: a concurrent call already made this exact change
        if stage > old:
            if not (by or "").strip():
                raise ValueError("raising a stage needs the name of the human (by)")
            if by.strip().lower() == "system":
                raise ValueError("only a human can raise a stage, not 'system'")
            if not evidence:
                raise ValueError("raising a stage needs recorded evidence")
        if row is None:
            s.add(Setting(key=key, value=str(stage)))
        else:
            row.value = str(stage)
        actor = "system" if not by or by.strip().lower() == "system" else f"human:{by}"
        # built directly (not via aecom.audit.audit) so the audit row commits in the same transaction as the write
        s.add(AuditLog(actor=actor, action="autonomy_stage", decision="promoted" if stage > old else "demoted",
                       detail=mask_detail({"tool": tool, "from": old, "to": stage, "reason": reason,
                                          "evidence": evidence})))
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
