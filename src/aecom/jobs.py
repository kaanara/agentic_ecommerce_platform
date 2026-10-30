"""UI-requested background jobs (backup now, verify, restore, doctor, and the Operations tab actions).

The dashboard only *enqueues* a row in ops_jobs; the scheduler process picks it up with ``run_pending`` on its tick and
runs the handler registered for the job kind, so heavy I/O never runs inside the Streamlit process. The handlers for
backup/verify/restore are registered by ``aecom.backup`` (wave 2); ``doctor`` is registered here; ``run``,
``sync_store``, ``reconcile``, ``monitors`` and ``agentmail_poll`` are registered by ``aecom.ops_actions``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from aecom.audit import audit
from aecom.db import Job, now, session

log = logging.getLogger(__name__)

KINDS = ("backup", "verify", "restore", "doctor", "update",
         "run", "sync_store", "reconcile", "monitors", "agentmail_poll")
Handler = Callable[[dict[str, Any]], dict[str, Any]]
_handlers: dict[str, Handler] = {}


def register(kind: str, handler: Handler) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    _handlers[kind] = handler


def _row(j: Job) -> dict[str, Any]:
    return {"id": j.id, "kind": j.kind, "status": j.status, "requested_by": j.requested_by, "params": j.params or {},
            "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at, "result": j.result}


def enqueue(kind: str, requested_by: str, params: dict[str, Any] | None = None) -> str:
    """Queue a job. A second job of the same kind is refused while one is queued or running."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    with session() as s:
        busy = s.scalars(select(Job).where(Job.kind == kind, Job.status.in_(("queued", "running")))).first()
        if busy is not None:
            raise ValueError(f"a {kind} job is already {busy.status}")
        job = Job(kind=kind, requested_by=requested_by, params=params or {})
        s.add(job)
        s.flush()
        job_id = job.id
    audit(f"human:{requested_by}" if requested_by else "system", f"job_{kind}_requested", "ok", {"job_id": job_id})
    return job_id


def record(kind: str, requested_by: str, result: dict[str, Any], status: str = "ok") -> str:
    """Write a job row already in its final state, for one-shot scripts that run outside the ``run_pending`` loop
    (``aecom-restore.service``, which runs while the scheduler itself is stopped)."""
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    t = now()
    with session() as s:
        job = Job(kind=kind, requested_by=requested_by, status=status, started_at=t, finished_at=t, result=result)
        s.add(job)
        s.flush()
        job_id = job.id
    audit(f"human:{requested_by}" if requested_by else "system", f"job_{kind}_recorded", status, {"job_id": job_id})
    return job_id


def get(job_id: str) -> dict[str, Any] | None:
    with session() as s:
        j = s.get(Job, job_id)
        return _row(j) if j else None


def list_jobs(kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    with session() as s:
        q = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if kind:
            q = q.where(Job.kind == kind)
        return [_row(j) for j in s.scalars(q)]


def run_pending() -> list[str]:
    """Run queued jobs one after another (scheduler tick). A handler exception marks the job 'error' and goes on."""
    ran: list[str] = []
    while True:
        with session() as s:
            job = s.scalars(select(Job).where(Job.status == "queued").order_by(Job.created_at)).first()
            if job is None:
                return ran
            job.status, job.started_at = "running", now()
            job_id, kind, params = job.id, job.kind, dict(job.params or {})
        handler = _handlers.get(kind)
        try:
            if handler is None:
                raise RuntimeError(f"no handler registered for {kind} jobs")
            result, status = handler(params) or {}, "ok"
        except Exception as exc:  # noqa: BLE001 - recorded on the job and in the error table, never raised to the scheduler
            log.exception("job %s (%s) failed", job_id, kind)
            result, status = {"error": f"{type(exc).__name__}: {exc}"[:500]}, "error"
        with session() as s:
            job = s.get(Job, job_id)
            job.status, job.finished_at, job.result = status, now(), result
        ran.append(job_id)


def _doctor(_params: dict[str, Any]) -> dict[str, Any]:
    from aecom import doctor

    return {"checks": doctor.run_checks()}


register("doctor", _doctor)
