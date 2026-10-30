import pytest

from aecom.policy.gate import evaluate, is_forbidden
from aecom.tools.catalog import CATALOG

REASON = "Forbidden for agents: accounts, logins and form submission are human-only."


@pytest.mark.parametrize("tool", [
    "sign_up", "user_signup", "create_account", "register_account", "login", "log_in", "sign_in",
    "submit_form", "enter_password", "add_payment_method",
    "mcp:playwright/browser_fill_form", "mcp:playwright/browser_type", "mcp:playwright/browser_click",
    "mcp:playwright/browser_file_upload", "mcp:shop/create_account", "mcp:x/SignIn", "mcp:auth/LOGIN",
])
@pytest.mark.parametrize("side_effect", [True, False])
def test_forbidden_patterns_deny_as_t4(tool, side_effect):
    r = evaluate("ceo", tool, {}, side_effect=side_effect)
    assert (r.decision, r.tier, r.reason) == ("deny", "T4", REASON)


@pytest.mark.parametrize("agent,tool,side_effect,expected", [
    ("researcher", "list_products", True, "allow"),
    ("researcher", "mcp:playwright/browser_navigate", False, "allow"),  # M2: read-only browser, researcher only
    ("anyone", "mcp:playwright/browser_navigate", True, "deny"),
    ("anyone", "mcp:somewhere/unclassified_tool", True, "escalate"),
    ("supply", "register_vendor", True, "allow"),
])
def test_non_matching_tools_follow_existing_rules(agent, tool, side_effect, expected):
    assert evaluate(agent, tool, {"monthly_cost_eur": 0}, side_effect=side_effect).decision == expected


def test_no_builtin_tool_matches_a_forbidden_pattern():
    assert not [name for name in CATALOG if is_forbidden(name)]
    assert not is_forbidden("register_vendor")
