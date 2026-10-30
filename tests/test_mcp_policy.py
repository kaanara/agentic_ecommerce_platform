"""F5: policy rules for MCP tools (``mcp:<server>/<tool>``), the server wildcard (``mcp:<server>/*``), the
``defaults.mcp_unclassified_tier`` fallback, and the destructive-name bump for a tool nobody classified."""

import asyncio

import pytest
from sqlalchemy import select

from aecom.db import AuditLog, session
from aecom.policy import versions as pv
from aecom.policy.gate import evaluate, load_policy, looks_destructive, resolve_rule, run_gated

POLICY_YAML = """
defaults:
  unknown_tool_tier: T3
  mcp_unclassified_tier: T3
  sandboxed_tool_tier: T3
  approval_ttl_hours: 72
rules:
  mcp:acme/create_refund:
    tier: T2
    agents: [support]
    limits: {amount_eur: 50}
    reason: "Refunds above €50 via acme need a human."
  mcp:acme/*: {tier: T1}
  mcp:danger/wipe_all: {tier: T4}
"""


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    """load_policy() is cached with no args; bust it around every test like test_policy_versions.py does, so a
    policy saved by one test never leaks into (or is missed by) the next."""
    load_policy.cache_clear()
    yield
    load_policy.cache_clear()


@pytest.fixture
def mcp_policy():
    """Activates POLICY_YAML as the live policy (aecom.policy.versions), same mechanism the dashboard uses."""
    pv.save_version(POLICY_YAML, "F5 test policy", "alice", step_up_ok=True)
    return load_policy()


# --------------------------------------------------------------------------- resolution order
def test_exact_tool_rule_wins_over_the_server_wildcard(mcp_policy):
    assert evaluate("support", "mcp:acme/create_refund", {"amount_eur": 10}).decision == "allow"
    over_limit = evaluate("support", "mcp:acme/create_refund", {"amount_eur": 500})
    assert over_limit.decision == "escalate"
    assert over_limit.tier == "T2"


def test_server_wildcard_applies_to_any_other_tool_on_that_server(mcp_policy):
    r = evaluate("marketing", "mcp:acme/list_items", {}, side_effect=True)
    assert (r.decision, r.tier) == ("allow", "T1")  # mcp:acme/* = T1, no agents restriction


def test_unclassified_mcp_tool_falls_back_to_defaults_mcp_unclassified_tier(mcp_policy):
    r = evaluate("marketing", "mcp:brand-new-vendor/frobnicate", {}, side_effect=True)
    assert (r.decision, r.tier) == ("escalate", "T3")
    # read-only (no side effect) stays T0/allow even when nobody classified the tool, like a builtin tool would
    r_ro = evaluate("marketing", "mcp:brand-new-vendor/frobnicate", {}, side_effect=False)
    assert (r_ro.decision, r_ro.tier) == ("allow", "T0")


def test_mcp_unclassified_tier_falls_back_to_unknown_tool_tier_when_absent():
    """A policy saved before F5 (no defaults.mcp_unclassified_tier) still gets a sane tier for a brand-new MCP
    tool: aecom.policy.gate.resolve_rule falls back to defaults.unknown_tool_tier."""
    policy = {"defaults": {"unknown_tool_tier": "T2", "sandboxed_tool_tier": "T3"}, "rules": {}}
    rule = resolve_rule("mcp:brand-new-vendor/frobnicate", policy, side_effect=True)
    assert rule["tier"] == "T2"


# --------------------------------------------------------------------------- destructive-name bump
@pytest.mark.parametrize("tool", [
    "mcp:brand-new-vendor/create_refund", "mcp:brand-new-vendor/delete_record", "mcp:brand-new-vendor/send_funds",
    "mcp:brand-new-vendor/pay_invoice", "mcp:brand-new-vendor/transfer_balance", "mcp:brand-new-vendor/charge_card",
    "mcp:brand-new-vendor/submit_order", "mcp:brand-new-vendor/purchase_item", "mcp:brand-new-vendor/download_report",
])
def test_destructive_looking_name_bumps_an_unclassified_mcp_tool_to_at_least_t3(mcp_policy, tool):
    assert looks_destructive(tool)
    r = evaluate("marketing", tool, {}, side_effect=True)
    assert (r.decision, r.tier) == ("escalate", "T3")
    assert "destructive" in r.reason.lower() or "financial" in r.reason.lower()


def test_destructive_name_bump_beats_a_read_only_flag(mcp_policy):
    """Even a tool marked read-only (no side effect) by a vendor's config is bumped: the name heuristic is a safety
    net for a tool nobody has looked at, so it never silently stays T0."""
    r = evaluate("marketing", "mcp:brand-new-vendor/delete_cache", {}, side_effect=False)
    assert (r.decision, r.tier) == ("escalate", "T3")


def test_non_destructive_unclassified_name_is_not_bumped(mcp_policy):
    assert not looks_destructive("mcp:brand-new-vendor/list_items")
    r = evaluate("marketing", "mcp:brand-new-vendor/list_items", {}, side_effect=True)
    assert r.tier == "T3"  # still the plain unclassified default, not further bumped
    assert "destructive" not in r.reason.lower()


def test_destructive_bump_is_logged(mcp_policy):
    """The escalation goes through the normal audit path (run_gated audits every non-T0/non-allow decision)."""
    async def execute(_args):
        return "unused"

    out = asyncio.run(run_gated("marketing", "mcp:brand-new-vendor/delete_cache", {}, execute, side_effect=True))
    assert out.startswith("ESCALATED") and "T3" in out
    with session() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.action == "mcp:brand-new-vendor/delete_cache")).all()
    assert rows and rows[-1].decision == "escalate"
    assert rows[-1].detail["tier"] == "T3"


# --------------------------------------------------------------------------- T4 hard denial
def test_t4_mcp_rule_is_a_hard_denial_regardless_of_agent(mcp_policy):
    for agent in ("ceo", "marketing", "support"):
        r = evaluate(agent, "mcp:danger/wipe_all", {}, side_effect=True)
        assert (r.decision, r.tier) == ("deny", "T4")
    # even read-only calls to a T4 tool are denied - T4 means forbidden for agents, not "forbidden unless read-only"
    assert evaluate("ceo", "mcp:danger/wipe_all", {}, side_effect=False).decision == "deny"


# --------------------------------------------------------------------------- agent allowlist
def test_agent_allowlist_is_respected_on_an_mcp_rule(mcp_policy):
    denied = evaluate("marketing", "mcp:acme/create_refund", {"amount_eur": 10})
    assert denied.decision == "deny"
    assert "support" in denied.reason
    assert evaluate("support", "mcp:acme/create_refund", {"amount_eur": 10}).decision == "allow"


# --------------------------------------------------------------------------- sandboxed servers stay read-only
def test_sandboxed_mcp_server_is_bumped_to_at_least_sandboxed_tool_tier(mcp_policy):
    """Even a classified T2 rule escalates when the call comes from a sandboxed server (F5: 'sandboxed servers stay
    read-only as today')."""
    r = evaluate("support", "mcp:acme/create_refund", {"amount_eur": 10}, sandboxed=True)
    assert r.decision == "escalate"
    assert r.tier == "T3"  # max(T2, sandboxed_tool_tier=T3)


# --------------------------------------------------------------------------- aecom.policy.versions: validation
def test_validate_yaml_rejects_an_unknown_mcp_unclassified_tier():
    with pytest.raises(ValueError, match="mcp_unclassified_tier"):
        pv.validate_yaml("defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3, mcp_unclassified_tier: WAT}\n"
                         "rules: {}")


def test_validate_yaml_accepts_a_policy_without_mcp_unclassified_tier():
    pv.validate_yaml("defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\nrules: {}")


def test_validate_yaml_rejects_a_t0_t1_wildcard_on_a_financial_looking_server():
    base = "defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\n"
    with pytest.raises(ValueError, match="acknowledge_financial_wildcard"):
        pv.validate_yaml(base + "rules: {mcp:stripe/*: {tier: T1}}")
    with pytest.raises(ValueError, match="acknowledge_financial_wildcard"):
        pv.validate_yaml(base + "rules: {mcp:acme-billing/*: {tier: T0}}")
    # a T2+ wildcard on the same server needs no override - only T0/T1 bypass human review
    pv.validate_yaml(base + "rules: {mcp:stripe/*: {tier: T2, limits: {amount_eur: 50}}}")
    # the explicit, comment-free override flag lets it through
    pv.validate_yaml(base + "rules: {mcp:stripe/*: {tier: T1, acknowledge_financial_wildcard: true}}")


def test_validate_yaml_does_not_flag_a_non_financial_wildcard_or_an_exact_mcp_rule():
    base = "defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\n"
    pv.validate_yaml(base + "rules: {mcp:acme/*: {tier: T1}}")
    pv.validate_yaml(base + "rules: {mcp:stripe/create_refund: {tier: T1}}")  # exact rule, not a wildcard


# --------------------------------------------------------------------------- aecom.policy.versions: loosens()
def test_loosens_detects_a_lowered_mcp_rule_tier():
    old = "defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\nrules: {mcp:acme/create_refund: {tier: T3}}"
    new = "defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\nrules: {mcp:acme/create_refund: {tier: T1}}"
    assert pv.loosens(old, new)
    assert not pv.loosens(new, old)  # tightening back is not a loosening


def test_loosens_detects_a_lowered_mcp_wildcard_limit():
    old = ("defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\n"
          "rules: {mcp:acme/*: {tier: T2, limits: {amount_eur: 50}}}")
    new = ("defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\n"
          "rules: {mcp:acme/*: {tier: T2, limits: {amount_eur: 500}}}")
    assert pv.loosens(old, new)


def test_loosens_detects_a_lowered_mcp_unclassified_default():
    old = "defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3, mcp_unclassified_tier: T3}\nrules: {}"
    new = "defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3, mcp_unclassified_tier: T1}\nrules: {}"
    assert pv.loosens(old, new)
    assert not pv.loosens(old, old)


def test_save_version_rejects_a_financial_wildcard_without_the_override_flag():
    base = "defaults: {unknown_tool_tier: T3, sandboxed_tool_tier: T3}\n"
    with pytest.raises(ValueError, match="acknowledge_financial_wildcard"):
        pv.save_version(base + "rules: {mcp:stripe/*: {tier: T0}}", "oops", "alice", step_up_ok=True)
