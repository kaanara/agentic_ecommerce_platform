"""D3 (args/result summaries in ops_tool_calls), I16 (masked audit details, dashboard mask_df), R4 (MCP server names
are not matched against forbidden patterns)."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

from aecom.audit import audit
from aecom.context import current_run
from aecom.db import AuditLog, ToolCallLog, session
from aecom.policy.gate import SUMMARY_MAX, evaluate, is_forbidden, run_gated
from aecom.scenarios import run_scenario

LICENSE = "LIC-AAAA-BBBB-CCCC-9Z7Q"
DOWNLOAD = "https://files.example.com/dl/secret-link-123"
AGENTMAIL = "am_us_supersecretvalue123"
TOKEN = "tok_live_abcdef123456"
SECRETS = [LICENSE, DOWNLOAD, AGENTMAIL, TOKEN, "secret-link-123", "supersecretvalue123", "abcdef123456"]


def _gated(agent, tool, args, execute, side_effect=True):
    token = current_run.set("run_d3")
    try:
        return asyncio.run(run_gated(agent, tool, args, execute, side_effect=side_effect))
    finally:
        current_run.reset(token)


def _calls() -> list[ToolCallLog]:
    with session() as s:
        return s.scalars(select(ToolCallLog).where(ToolCallLog.run_id == "run_d3").order_by(ToolCallLog.id)).all()


def _stored_text() -> str:
    with session() as s:
        calls = s.scalars(select(ToolCallLog)).all()
        audits = s.scalars(select(AuditLog)).all()
        return " ".join([f"{c.args_summary} {c.result_summary}" for c in calls]
                        + [json.dumps(a.detail, default=str) for a in audits])


def test_summaries_for_allowed_escalated_denied_and_error_calls():
    def boom(args):
        raise RuntimeError(f"upstream refused key {AGENTMAIL}")

    allowed = _gated("support", "get_order", {"order_id": "o-1"},
                     lambda a: {"order_id": "o-1", "license_key": LICENSE, "download_url": DOWNLOAD}, side_effect=False)
    escalated = _gated("support", "refund_order", {"order_id": "o-1", "amount_eur": 500, "reason": f"Bearer {TOKEN}"},
                       lambda a: pytest.fail("escalated calls must not execute"))
    denied = _gated("ceo", "login", {"user": "x", "password": "hunter2", "token": TOKEN}, lambda a: None)
    errored = _gated("support", "get_order", {"order_id": "o-2", "api_key": AGENTMAIL}, boom, side_effect=False)

    rows = _calls()
    assert [(r.tool_name, r.decision, r.ok) for r in rows] == [
        ("get_order", "allow", True), ("refund_order", "escalate", True), ("login", "deny", True),
        ("get_order", "allow", True), ("get_order", "error", False)]
    by = {r.decision: r for r in rows}
    assert json.loads(rows[0].args_summary) == {"order_id": "o-1"}
    assert json.loads(rows[0].result_summary) == {"order_id": "o-1", "license_key": "****9Z7Q",
                                                  "download_url": "(sent to the customer)"}
    assert by["escalate"].result_summary.startswith("ESCALATED to human approval (T")
    assert '"amount_eur": 500' in by["escalate"].args_summary
    assert by["deny"].result_summary == denied and denied.startswith("DENIED by policy (T4)")
    assert json.loads(by["deny"].args_summary) == {"user": "x", "password": "***", "token": "***"}
    assert by["error"].result_summary == errored and errored.startswith("ERROR: RuntimeError")
    assert rows[3].result_summary == errored  # the call row also records how it ended
    assert all(r.args_summary and r.result_summary for r in rows)
    assert allowed  # the model still sees the (tool-redacted) result

    stored = _stored_text()
    assert not [s for s in SECRETS if s in stored]
    assert "hunter2" not in stored


def test_summaries_are_shortened():
    _gated("support", "get_order", {"order_id": "x" * 5000}, lambda a: "y" * 5000, side_effect=False)
    row = _calls()[0]
    assert len(row.args_summary) <= SUMMARY_MAX and len(row.result_summary) <= SUMMARY_MAX
    assert row.result_summary.endswith("…")


def test_json_string_results_are_redacted_too():
    """MCP tools return text; a JSON payload inside it still gets its secret keys redacted."""
    _gated("support", "get_order", {"order_id": "o"},
           lambda a: json.dumps({"access_token": TOKEN, "items": [{"license_key": LICENSE}]}), side_effect=False)
    assert json.loads(_calls()[0].result_summary) == {"access_token": "***", "items": [{"license_key": "****9Z7Q"}]}


def test_a_scripted_run_stores_summaries():
    res = asyncio.run(run_scenario("support-refund", offline=True))
    with session() as s:
        rows = s.scalars(select(ToolCallLog).where(ToolCallLog.run_id == res["ctx"]["run_id"])).all()
    assert rows and all(r.args_summary is not None and r.result_summary is not None for r in rows)
    assert not [s for s in SECRETS if s in _stored_text()]


def test_audit_masks_nested_details():
    audit("support", "x", "allow", {"args": {"order": {"license_key": LICENSE, "note": f"key {AGENTMAIL}",
                                                       "headers": [{"Authorization": f"Bearer {TOKEN}"}]}},
                                    "download_url": DOWNLOAD, "client_secret": "s3cr3t-value", "amount": 5})
    with session() as s:
        detail = s.scalars(select(AuditLog)).all()[-1].detail
    assert detail == {"args": {"order": {"license_key": "****9Z7Q", "note": "key ***",
                                         "headers": [{"Authorization": "***"}]}},
                      "download_url": "(sent to the customer)", "client_secret": "***", "amount": 5}


def _common():
    path = Path(__file__).resolve().parents[1] / "dashboard" / "_common.py"
    spec = importlib.util.spec_from_file_location("dashboard_common_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mask_df_masks_only_the_given_columns():
    df = pd.DataFrame({"detail": [json.dumps({"token": TOKEN, "n": 1}), None],
                       "body": [f"Your access: {AGENTMAIL}", "hello"],
                       "subject": [AGENTMAIL, "plain"]})
    out = _common().mask_df(df, ["detail", "body", "missing"])
    assert json.loads(out["detail"][0]) == {"token": "***", "n": 1} and pd.isna(out["detail"][1])
    assert list(out["body"]) == ["Your access: ***", "hello"]
    assert list(out["subject"]) == [AGENTMAIL, "plain"]  # not asked for
    assert df["body"][0] == f"Your access: {AGENTMAIL}"  # the input frame is untouched


@pytest.mark.parametrize("tool,forbidden", [
    ("mcp:login_service/list_users", False), ("mcp:signup-portal/get_status", False),
    ("mcp:x/login", True), ("mcp:login_service/login", True), ("login", True), ("list_users", False),
])
def test_forbidden_patterns_match_only_the_mcp_tool_part(tool, forbidden):
    assert is_forbidden(tool) is forbidden
    r = evaluate("ceo", tool, {}, side_effect=False)
    assert (r.decision == "deny" and r.tier == "T4") is forbidden
