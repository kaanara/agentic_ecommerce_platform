"""Approvals respect the kill switch: approve is refused (and stays pending), reject is still allowed."""

import pytest

from aecom import approvals
from aecom.company_settings import set_setting
from aecom.db import Approval, session
from aecom.tools.catalog import CATALOG, ToolDef


@pytest.fixture
def parked(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setitem(CATALOG, "fake_tool", ToolDef("fake_tool", lambda **kw: calls.append(kw) or {"ok": True},
                                                      "test", True, False))
    with session() as s:
        a = Approval(agent_id="cfo", tool_name="fake_tool", args={"x": 1}, risk_tier="T2", reason="test")
        s.add(a)
        s.flush()
        apr_id = a.id
    return apr_id, calls


def _status(apr_id: str) -> str:
    with session() as s:
        return s.get(Approval, apr_id).status


def test_approve_refused_while_kill_switch_on_then_runs_after(parked):
    apr_id, calls = parked
    set_setting("kill_switch", "on")
    with pytest.raises(ValueError, match="kill switch is on"):
        approvals.approve(apr_id, "tester")
    assert _status(apr_id) == "pending" and calls == []
    set_setting("kill_switch", "off")
    assert approvals.approve(apr_id, "tester")["status"] == "executed"
    assert calls == [{"x": 1}] and _status(apr_id) == "executed"


def test_reject_allowed_while_kill_switch_on(parked):
    apr_id, calls = parked
    set_setting("kill_switch", "on")
    assert approvals.reject(apr_id, "tester", "no")["status"] == "rejected"
    assert _status(apr_id) == "rejected" and calls == []


def test_edits_before_approval_are_recorded_for_the_trust_report():
    """G4: an approval the human changed is counted as 'edited' and keeps what the agent originally asked for."""
    from aecom.db import Approval, session

    def park(amount):
        with session() as s:
            a = Approval(agent_id="support", tool_name="record_expense", args={"account": "vendor_expense", "amount_eur": amount, "memo": "m"},
                         risk_tier="T2", reason="r", context="")
            s.add(a)
            s.flush()
            return a.id

    unchanged, edited, same_value = park(250.0), park(300.0), park(260.0)
    approvals.approve(unchanged, "kaan")
    approvals.approve(edited, "kaan", args_override={"amount_eur": 200.0})
    approvals.approve(same_value, "kaan", args_override={"amount_eur": 260.0})  # 'edited' to the same value: not an edit
    with session() as s:
        u, e, sv = (s.get(Approval, i) for i in (unchanged, edited, same_value))
        assert (u.args_edited, u.original_args) == (False, None)
        assert e.args_edited is True and e.original_args["amount_eur"] == 300.0 and e.args["amount_eur"] == 200.0
        assert sv.args_edited is False
