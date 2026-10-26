"""HTTP API: runs, approvals (HITL), kill switch, events, AgentMail webhook.

    uv run aecom api      → http://127.0.0.1:8000/docs

Handlers are sync on purpose: FastAPI runs them in a thread pool, so the agent/event code (which uses
asyncio.run internally for approvals and reflexes) never collides with the server's event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

import aecom.handlers  # noqa: F401  (registers event subscribers)
from aecom import approvals
from aecom.bootstrap import init
from aecom.bus.events import drain, publish
from aecom.company_settings import kill_switch_on, set_setting
from aecom.db import new_id

app = FastAPI(title="aecom — agentic e-commerce company", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init()


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


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "kill_switch": kill_switch_on()}


@app.get("/approvals")
def list_approvals(status: str | None = "pending") -> list[dict[str, Any]]:
    return approvals.list_approvals(status)


@app.post("/approvals/{approval_id}/approve")
def approve(approval_id: str, body: Decision) -> dict[str, Any]:
    try:
        result = approvals.approve(approval_id, body.by, body.note, body.args_override)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    drain()
    return result


@app.post("/approvals/{approval_id}/reject")
def reject(approval_id: str, body: Decision) -> dict[str, Any]:
    try:
        return approvals.reject(approval_id, body.by, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/kill-switch")
def kill_switch(on: bool) -> dict[str, Any]:
    set_setting("kill_switch", "on" if on else "off")
    return {"kill_switch": kill_switch_on()}


def _run_company(directive: str, run_id: str) -> None:
    from aecom.graph.supervisor import run_company

    asyncio.run(run_company(directive, run_id=run_id))


def _run_selection(req: SelectionRequest, run_id: str) -> None:
    from aecom.graph.selection import run_selection

    asyncio.run(run_selection(req.category, req.need, req.candidates, run_id=run_id))


@app.post("/runs")
def start_run(req: RunRequest, bg: BackgroundTasks) -> dict[str, str]:
    run_id = new_id("run")
    bg.add_task(_run_company, req.directive, run_id)
    return {"run_id": run_id, "status": "started"}


@app.post("/selections")
def start_selection(req: SelectionRequest, bg: BackgroundTasks) -> dict[str, str]:
    run_id = new_id("sel")
    bg.add_task(_run_selection, req, run_id)
    return {"run_id": run_id, "status": "started"}


@app.post("/events")
def post_event(evt: EventIn, bg: BackgroundTasks) -> dict[str, Any]:
    event_id = publish(evt.topic, evt.payload, source="api")
    bg.add_task(drain)
    return {"event_id": event_id}


@app.post("/webhooks/agentmail")
def agentmail_webhook(payload: dict[str, Any], bg: BackgroundTasks) -> dict[str, Any]:
    """AgentMail `message.received` webhook → email.received event for the Support agent."""
    msg = payload.get("message", payload)
    sender = msg.get("from") or msg.get("from_", "")
    if isinstance(sender, list):
        sender = sender[0] if sender else ""
    event_id = publish("email.received", {"from": sender, "subject": msg.get("subject", ""),
                                          "body": msg.get("extracted_text") or msg.get("text") or "",
                                          "message_id": msg.get("message_id"), "thread_id": msg.get("thread_id"),
                                          "labels": msg.get("labels", [])}, source="agentmail")
    bg.add_task(drain)
    return {"event_id": event_id}
