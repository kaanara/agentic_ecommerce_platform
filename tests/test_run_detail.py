"""D1/D2: running-now panel and per-run timeline (aecom.run_detail)."""

import asyncio
from datetime import timedelta

from aecom import approvals, run_detail
from aecom.db import AgentRun, ErrorRow, Incident, ToolCallLog, now, session
from aecom.observability import record_error
from aecom.scenarios import run_scenario


def _add_run(run_id: str, agent: str, started_ago: timedelta, status: str = "running") -> str:
    with session() as s:
        r = AgentRun(run_id=run_id, agent_id=agent, model="scripted", task="do things", status=status,
                     started_at=now() - started_ago)
        s.add(r)
        s.flush()
        return r.id


def test_running_now_flags_stuck_runs():
    _add_run("run_fresh", "support", timedelta(minutes=2))
    _add_run("run_old", "marketing", timedelta(hours=2))
    _add_run("run_done", "finance", timedelta(hours=3), status="ok")
    rows = run_detail.running_now()
    assert [r["run_id"] for r in rows] == ["run_old", "run_fresh"]  # oldest first, finished runs excluded
    stuck = {r["run_id"]: r["stuck"] for r in rows}
    assert stuck == {"run_old": True, "run_fresh": False}
    assert rows[0]["age_s"] >= 2 * 3600 - 1
    assert all(not r["stuck"] for r in run_detail.running_now(stuck_after=timedelta(hours=5)))


def _refund_run() -> dict:
    res = asyncio.run(run_scenario("support-refund", offline=True))
    return res["ctx"] | {"approvals": res["result"]}


def test_timeline_of_a_scripted_run_is_ordered():
    ctx = _refund_run()
    tl = run_detail.timeline(ctx["run_id"])
    kinds = [e["kind"] for e in tl]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert tl == sorted(tl, key=lambda e: e["ts"])
    calls = [e for e in tl if e["kind"] == "tool_call"]
    assert [c["summary"].split(" ")[0] for c in calls] == ["get_order", "refund_order", "send_email"]
    created = next(i for i, e in enumerate(tl) if e["kind"] == "approval_created")
    refund = next(i for i, e in enumerate(tl) if e["kind"] == "tool_call" and e["summary"].startswith("refund_order"))
    send = next(i for i, e in enumerate(tl) if e["kind"] == "tool_call" and e["summary"].startswith("send_email"))
    assert refund < created < send
    assert tl[created]["ref"] == ctx["approvals"][0] and "refund_order" in tl[created]["summary"]
    assert {"ts", "kind", "actor", "summary", "ref"} == set(tl[0])

    approvals.reject(ctx["approvals"][0], "kaan", "not this time")
    tl = run_detail.timeline(ctx["run_id"])
    decided = [e for e in tl if e["kind"] == "approval_decided"]
    assert len(decided) == 1 and decided[0]["actor"] == "kaan" and "rejected" in decided[0]["summary"]


def test_timeline_accepts_an_agent_run_row_id():
    ctx = _refund_run()
    with session() as s:
        row_id = s.query(AgentRun).filter_by(run_id=ctx["run_id"]).one().id
    assert run_detail.timeline(row_id) == run_detail.timeline(ctx["run_id"])


def test_summary_counts():
    ctx = _refund_run()
    s = run_detail.summary(ctx["run_id"])
    assert s["agents"] == ["support"]
    assert s["tools_used"] == ["get_order", "refund_order", "send_email"]
    assert s["tool_calls"] == 3 and s["approvals"] == 1 and s["errors"] == 0
    assert s["status"] == "ok" and s["cost_eur"] >= 0
    assert run_detail.summary("run_nope")["status"] == "unknown"


def test_summary_and_timeline_include_errors_and_incidents():
    _add_run("run_x", "support", timedelta(minutes=1))
    record_error("gate", RuntimeError("boom"), run_id="run_x")
    with session() as s:
        s.add(Incident(monitor="stuck_run", severity="P2", subject="run_x", detail="running for 40 min"))
        s.add(ToolCallLog(run_id="run_x", agent_id="support", tool_name="get_order", decision="allow",
                          args_summary='{"order_id": "ord_1"}', result_summary='{"status": "paid"}'))
    s = run_detail.summary("run_x")
    assert s["errors"] == 1 and s["status"] == "running"
    tl = run_detail.timeline("run_x")
    kinds = {e["kind"] for e in tl}
    assert {"run_start", "error", "incident", "tool_call"} <= kinds and "run_end" not in kinds
    call = next(e for e in tl if e["kind"] == "tool_call")
    assert "ord_1" in call["summary"] and "paid" in call["summary"]


def test_secrets_in_error_rows_stay_masked(monkeypatch):
    monkeypatch.setenv("SOME_VENDOR_TOKEN", "vendor-token-value-42")
    _add_run("run_s", "support", timedelta(minutes=1), status="error")
    record_error("gate", RuntimeError("auth failed with vendor-token-value-42"), run_id="run_s")
    with session() as s:  # a row written without masking (e.g. by older code) is masked on display too
        s.add(ErrorRow(component="agent", run_id="run_s", exc_type="ValueError",
                       message="bad key sk-ant-abcdef123456 and vendor-token-value-42"))
        s.add(ToolCallLog(run_id="run_s", agent_id="support", tool_name="http_get", decision="allow",
                          args_summary="Authorization: Bearer abc.def.ghi"))
    tl = run_detail.timeline("run_s")
    text = " ".join(e["summary"] for e in tl)
    assert "vendor-token-value-42" not in text and "sk-ant-abcdef123456" not in text and "abc.def.ghi" not in text
    assert text.count("***") >= 3
    assert run_detail.summary("run_s")["errors"] == 2


def test_recent_run_ids_groups_agents():
    _add_run("run_a", "ceo", timedelta(minutes=5), status="ok")
    _add_run("run_a", "finance", timedelta(minutes=4), status="ok")
    _add_run("run_b", "support", timedelta(minutes=1), status="ok")
    rows = run_detail.recent_run_ids()
    assert [r["run_id"] for r in rows] == ["run_b", "run_a"]
    assert sorted(rows[1]["agents"]) == ["ceo", "finance"]
