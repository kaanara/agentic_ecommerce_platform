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
