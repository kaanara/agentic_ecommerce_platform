"""UI-edited policy versions (``ops_policy_versions``). ``aecom.policy.gate.load_policy`` loads ``active_yaml()``
when a version is active, else falls back to the file shipped in git.

Every saved or activated version must pass ``validate_yaml`` (the same shape ``aecom.policy.gate.evaluate`` reads;
this includes ``mcp:<server>/<tool>`` and ``mcp:<server>/*`` rules, F5). ``loosens(old, new)`` is true when an
existing rule (a built-in or an MCP one) gets a lower tier, a higher limit, a dropped monthly budget cap, or any
field of a T4 rule changes, or when ``defaults.unknown_tool_tier``, ``defaults.sandboxed_tool_tier`` or (F5)
``defaults.mcp_unclassified_tier`` drops a tier - saving or activating such a version needs ``step_up_ok=True`` (the
dashboard password again), matching the "loosens a rule" step-up case in the settings plan.
"""

from __future__ import annotations

from typing import Any

import yaml

from aecom.audit import audit
from aecom.config import get_settings
from aecom.db import PolicyVersion, now, session

# Mirrors aecom.policy.gate.TIER_ORDER, duplicated here because gate.load_policy falls back to this module
# (importing gate at module scope here would be circular).
TIER_ORDER = ["T0", "T1", "T2", "T3", "T4"]

# F5: name fragments that make an MCP server "look financial" for the wildcard-override check below. Mirrors the
# spirit of aecom.policy.gate.MCP_DESTRUCTIVE_NAME_HINTS but at the server level, not the tool level.
FINANCIAL_SERVER_HINTS = ("stripe", "paypal", "pay", "bank", "billing", "invoice", "checkout", "finance", "wallet",
                         "ledger")


def _file_yaml() -> str:
    return get_settings().policy_file.read_text()


def _looks_financial_server(server: str) -> bool:
    name = server.lower()
    return any(hint in name for hint in FINANCIAL_SERVER_HINTS)


def validate_yaml(yaml_text: str) -> dict[str, Any]:
    """Parse and sanity-check a candidate policy.yaml. Raises ValueError (message fit for the UI) if it would break
    the gate: bad YAML, no ``defaults``/``rules`` mapping, an unknown tier, a non-numeric limit, or (F5) a
    ``mcp:<server>/*`` wildcard that would allow T0/T1 on a financial-looking server without an explicit
    ``acknowledge_financial_wildcard: true`` on that rule (a real field, not a YAML comment - comments don't survive
    parsing so they can't be checked)."""
    try:
        policy = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from None
    if not isinstance(policy, dict):
        raise ValueError("the policy must be a YAML mapping")
    defaults = policy.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError("defaults: is required")
    for key in ("unknown_tool_tier", "sandboxed_tool_tier"):
        if defaults.get(key) not in TIER_ORDER:
            raise ValueError(f"defaults.{key} must be one of {TIER_ORDER}")
    if "mcp_unclassified_tier" in defaults and defaults["mcp_unclassified_tier"] not in TIER_ORDER:
        raise ValueError(f"defaults.mcp_unclassified_tier must be one of {TIER_ORDER}")
    rules = policy.get("rules")
    if not isinstance(rules, dict):
        raise ValueError("rules: is required")
    for name, rule in rules.items():
        if not isinstance(rule, dict) or rule.get("tier") not in TIER_ORDER:
            raise ValueError(f"rules.{name}.tier must be one of {TIER_ORDER}")
        if "agents" in rule and not isinstance(rule["agents"], list):
            raise ValueError(f"rules.{name}.agents must be a list")
        for arg, maximum in (rule.get("limits") or {}).items():
            try:
                float(maximum)
            except (TypeError, ValueError):
                raise ValueError(f"rules.{name}.limits.{arg} must be a number") from None
        if name.startswith("mcp:") and name.endswith("/*") and rule["tier"] in ("T0", "T1"):
            server = name[len("mcp:"):-len("/*")]
            if _looks_financial_server(server) and not rule.get("acknowledge_financial_wildcard"):
                raise ValueError(f"rules.{name}: a T0/T1 wildcard on a financial-looking server needs "
                                 "acknowledge_financial_wildcard: true")
    return policy


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rule_loosens(old_rule: dict[str, Any] | None, new_rule: dict[str, Any]) -> bool:
    if old_rule is None:
        return False  # a brand-new rule does not loosen an existing one
    if old_rule.get("tier") == "T4" and new_rule != old_rule:
        return True  # any change to a forbidden rule needs a human's eyes on it
    old_tier, new_tier = old_rule.get("tier"), new_rule.get("tier")
    if old_tier in TIER_ORDER and new_tier in TIER_ORDER and TIER_ORDER.index(new_tier) < TIER_ORDER.index(old_tier):
        return True
    old_limits, new_limits = old_rule.get("limits") or {}, new_rule.get("limits") or {}
    for arg, new_max in new_limits.items():
        old_max = _num(old_limits.get(arg))
        new_val = _num(new_max)
        if old_max is not None and new_val is not None and new_val > old_max:
            return True
    old_pct, new_pct = _num(old_rule.get("price_change_pct_max")), _num(new_rule.get("price_change_pct_max"))
    if old_pct is not None and new_pct is not None and new_pct > old_pct:
        return True
    if old_rule.get("monthly_budget_setting") and not new_rule.get("monthly_budget_setting"):
        return True  # a spending cap removed entirely
    return False


def loosens(old_yaml: str, new_yaml: str) -> bool:
    """True when ``new_yaml`` is less restrictive than ``old_yaml`` for an agent somewhere."""
    old = yaml.safe_load(old_yaml) or {}
    new = yaml.safe_load(new_yaml) or {}
    old_rules, new_rules = old.get("rules") or {}, new.get("rules") or {}
    for name, new_rule in new_rules.items():
        if _rule_loosens(old_rules.get(name), new_rule):
            return True
    for name, old_rule in old_rules.items():
        if name not in new_rules and old_rule.get("tier") in ("T3", "T4"):
            return True  # a high-stakes rule dropped outright
    old_defaults, new_defaults = old.get("defaults") or {}, new.get("defaults") or {}
    for key in ("unknown_tool_tier", "sandboxed_tool_tier", "mcp_unclassified_tier"):
        o, n = old_defaults.get(key), new_defaults.get(key)
        if o in TIER_ORDER and n in TIER_ORDER and TIER_ORDER.index(n) < TIER_ORDER.index(o):
            return True  # F5: lowering the MCP-unclassified default (same treatment as unknown_tool_tier)
    if set(old_defaults.get("forbidden_patterns") or []) - set(new_defaults.get("forbidden_patterns") or []):
        return True  # a forbidden pattern removed
    return False


def _clear_gate_cache() -> None:
    from aecom.policy.gate import load_policy

    load_policy.cache_clear()


def active_yaml() -> str | None:
    """The currently active UI-saved version's YAML, or None - the gate then falls back to the file."""
    with session() as s:
        row = s.query(PolicyVersion).filter_by(active=True).one_or_none()
        return row.yaml if row else None


def save_version(yaml_text: str, note: str, by: str, *, step_up_ok: bool = False) -> int:
    """Validate, store as a new version and make it the active one. Raises ``ValueError`` for invalid YAML and
    ``PermissionError`` if it loosens the policy and ``step_up_ok`` was not given."""
    if not by.strip():
        raise ValueError("a name is needed to record who changed this")
    validate_yaml(yaml_text)
    previous = active_yaml() or _file_yaml()
    if loosens(previous, yaml_text) and not step_up_ok:
        raise PermissionError("This change loosens the policy (a lower tier, a higher limit, a dropped budget cap, "
                              "or a T4 rule change) and needs your dashboard password.")
    with session() as s:
        s.query(PolicyVersion).filter(PolicyVersion.active.is_(True)).update({"active": False})
        row = PolicyVersion(yaml=yaml_text, note=note, created_by=by, created_at=now(), active=True)
        s.add(row)
        s.flush()
        version_id = row.id
    _clear_gate_cache()
    audit(f"human:{by}", "policy_version_save", "ok", {"version_id": version_id, "note": note})
    return version_id


def list_versions(limit: int = 50) -> list[dict[str, Any]]:
    with session() as s:
        rows = s.query(PolicyVersion).order_by(PolicyVersion.id.desc()).limit(limit)
        return [{"id": r.id, "note": r.note, "created_by": r.created_by, "created_at": r.created_at,
                 "active": r.active, "yaml": r.yaml} for r in rows]


def activate(version_id: int, by: str, *, step_up_ok: bool = False) -> None:
    """Roll back (or forward) to an existing version, replacing whichever is active now."""
    if not by.strip():
        raise ValueError("a name is needed to record who changed this")
    with session() as s:
        target = s.get(PolicyVersion, version_id)
        if target is None:
            raise ValueError(f"no policy version {version_id}")
        previous = active_yaml() or _file_yaml()
        if loosens(previous, target.yaml) and not step_up_ok:
            raise PermissionError("Activating this version loosens the policy and needs your dashboard password.")
        s.query(PolicyVersion).filter(PolicyVersion.active.is_(True)).update({"active": False})
        target.active = True
    _clear_gate_cache()
    audit(f"human:{by}", "policy_version_activate", "ok", {"version_id": version_id})
