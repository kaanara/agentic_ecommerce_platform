"""HTTP API: runs, approvals (HITL), kill switch, agent pause, safe mode, events, reports, provider webhooks.

    uv run aecom api      → http://127.0.0.1:8000/docs

Every route except /health and /webhooks/* requires `Authorization: Bearer $AECOM_API_TOKEN`. The webhooks
(`aecom.api.webhooks`) verify the provider's signature instead and fail closed.

Handlers are sync on purpose: FastAPI runs them in a thread pool, so the agent/event code (which uses
asyncio.run internally for approvals and reflexes) never collides with the server's event loop.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel

import aecom.handlers  # noqa: F401  (registers event subscribers)
from aecom import approvals
from aecom.api import webhooks
from aecom.bootstrap import init
from aecom.bus.events import drain, publish
from aecom.company_settings import agents_paused_on, enter_safe_mode, kill_switch_on, leave_safe_mode, safe_mode_on, set_flag
from aecom.db import new_id
from aecom.secrets import secrets


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token guard. Fails closed: no AECOM_API_TOKEN configured → 503."""
    expected = secrets.get("AECOM_API_TOKEN")
    if not expected:
        raise HTTPException(503, "API token not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), expected.encode()):
        raise HTTPException(401, "Invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    init()
    yield


app = FastAPI(title="aecom — agentic e-commerce company", version="0.1.0", lifespan=_lifespan)
api = APIRouter(dependencies=[Depends(require_token)])  # protected routes; included at the bottom


class Decision(BaseModel):
    by: str
    note: str = ""
    args_override: dict[str, Any] | None = None


class RunRequest(BaseModel):
    directive: str


class SelectionRequest(BaseModel):
    category: str
    need: str
    candidates: list[str] = []


class EventIn(BaseModel):
    topic: str
    payload: dict[str, Any] = {}


def _db_ok() -> bool:
    try:
        from sqlalchemy import text

        from aecom.db import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@app.get("/health")
def health(response: Response, detail: bool = True) -> dict[str, Any]:
    """Unauthenticated liveness and real state. HTTP 503 when the DB is unreachable or a P1 incident is active.

    Returns ok, db, kill_switch, agents_paused, safe_mode, open_incidents and event_bus; `?detail=false` returns
    only ok, kill_switch and agents_paused (for simple uptime checkers).
    """
    from aecom.bus.events import status
    from aecom.incidents import open_counts

    out: dict[str, Any] = {"ok": False, "db": _db_ok(), "kill_switch": None, "agents_paused": None,
                           "safe_mode": None, "open_incidents": None, "event_bus": None}
    if out["db"]:
        try:
            out.update(kill_switch=kill_switch_on(), agents_paused=agents_paused_on(),
                       safe_mode=safe_mode_on(), open_incidents=open_counts(), event_bus=status())
        except Exception:
            out["db"] = False
    out["ok"] = bool(out["db"] and out["open_incidents"] and out["open_incidents"].get("P1", 0) == 0)
    if not out["ok"]:
        response.status_code = 503
    if not detail:
        return {k: out[k] for k in ("ok", "kill_switch", "agents_paused")}
    return out


@api.get("/approvals")
def list_approvals(status: str | None = "pending") -> list[dict[str, Any]]:
    return approvals.list_approvals(status)


@api.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, body: Decision) -> dict[str, Any]:
    try:
        result = approvals.approve(approval_id, body.by, body.note, body.args_override)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    drain()
    return result


@api.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, body: Decision) -> dict[str, Any]:
    try:
        return approvals.reject(approval_id, body.by, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@api.post("/kill-switch")
def kill_switch(on: bool) -> dict[str, Any]:
    set_flag("kill_switch", on, "api")
    return {"kill_switch": kill_switch_on()}


@api.post("/agents-pause")
def agents_pause(on: bool) -> dict[str, Any]:
    set_flag("agents_paused", on, "api")
    return {"agents_paused": agents_paused_on()}


@api.post("/safe-mode")
def safe_mode(on: bool, by: str = "") -> dict[str, Any]:
    """Safe mode on (by hand) or off. The monitors only ever switch it on; leaving it is this, the CLI or the dashboard.
    Leaving requires `by` (the person's name), so the audit trail says who trusted the system again."""
    if not on and not by.strip():
        raise HTTPException(400, "by (your name) is required to leave safe mode")
    if on:
        enter_safe_mode(f"switched on by hand ({by})")
    else:
        leave_safe_mode(by)
    return {"safe_mode": safe_mode_on()}


def _run_company(directive: str, run_id: str) -> None:
    from aecom.graph.supervisor import run_company

    asyncio.run(run_company(directive, run_id=run_id))


def _run_selection(req: SelectionRequest, run_id: str) -> None:
    from aecom.graph.selection import run_selection

    asyncio.run(run_selection(req.category, req.need, req.candidates, run_id=run_id))


@api.post("/runs")
def start_run(req: RunRequest, bg: BackgroundTasks) -> dict[str, str]:
    run_id = new_id("run")
    bg.add_task(_run_company, req.directive, run_id)
    return {"run_id": run_id, "status": "started"}


@api.post("/selections")
def start_selection(req: SelectionRequest, bg: BackgroundTasks) -> dict[str, str]:
    run_id = new_id("sel")
    bg.add_task(_run_selection, req, run_id)
    return {"run_id": run_id, "status": "started"}


@api.post("/events")
def post_event(evt: EventIn, bg: BackgroundTasks) -> dict[str, Any]:
    event_id = publish(evt.topic, evt.payload, source="api")
    bg.add_task(drain)
    return {"event_id": event_id}


@api.get("/reports")
def list_reports(kind: str | None = None, limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    """Stored report versions, newest first (data, text, sha256, version)."""
    from aecom import reports

    return reports.list_reports(kind or None, limit=limit)


@api.get("/reports/{report_id}")
def get_report(report_id: str) -> dict[str, Any]:
    from aecom import reports

    if (report := reports.get_report(report_id)) is None:
        raise HTTPException(404, "report not found")
    return report


app.include_router(api)
app.include_router(webhooks.router)  # signature-verified instead of the bearer token
