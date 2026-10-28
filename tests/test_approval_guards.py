"""R8: racing identical escalations, expiry sweep + scheduler job, expired approvals over the API."""

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aecom import approvals, db, scheduler
from aecom.api import app as api_app
from aecom.db import Approval, session
from aecom.policy import gate
from aecom.policy.gate import run_gated

ARGS = {"platform": "x", "text": "hi"}


def _escalate(args=ARGS) -> str:
    return asyncio.run(run_gated("marketing", "publish_social_post", args, lambda a: None, side_effect=True))


def _count() -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(Approval))


def test_racing_identical_escalation_returns_same_id_and_one_row(monkeypatch):
    first = _escalate()
    (a,) = approvals.list_approvals("pending")
    real = gate._pending_row
    calls = []

    def miss_once(s, key):  # the pre-insert SELECT misses the row a concurrent writer just inserted
        calls.append(1)
        return None if len(calls) == 1 else real(s, key)

    monkeypatch.setattr(gate, "_pending_row", miss_once)
    second = _escalate()
    assert len(calls) == 2
    assert f"id={a['id']}" in first and f"id={a['id']}" in second and "already pending" in second
    assert _count() == 1


def test_expire_sweep_and_scheduler_job():
    _escalate()
    _escalate({"platform": "x", "text": "fresh"})
    with session() as s:
        s.scalars(select(Approval).order_by(Approval.created_at)).first().expires_at = db.now() - timedelta(minutes=1)
    assert scheduler.expire_approvals() == 1
    with session() as s:
        assert sorted(a.status for a in s.scalars(select(Approval))) == ["expired", "pending"]
    job = scheduler.build_scheduler().get_job("expire_approvals")
    assert job is not None and job.trigger.interval == timedelta(minutes=30)


def test_api_lists_expired_with_expires_at(monkeypatch):
    monkeypatch.setattr(api_app.secrets, "get", lambda name: "tok" if name == "AECOM_API_TOKEN" else None)
    _escalate()
    with session() as s:
        s.scalars(select(Approval)).first().expires_at = db.now() - timedelta(hours=1)
    c = TestClient(api_app.app)
    r = c.get("/approvals", params={"status": "expired"}, headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    (row,) = r.json()
    assert row["status"] == "expired" and row["expires_at"]
    assert c.get("/approvals", headers={"Authorization": "Bearer tok"}).json() == []
