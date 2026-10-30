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
import json
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import aecom.handlers  # noqa: F401  (registers event subscribers)
from aecom import approvals, chat, conversations, settings_registry
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


@app.middleware("http")
async def _config_refresh(request: Request, call_next: Any) -> Any:
    """Cheap per-request check: another process (dashboard, CLI, scheduler) may have changed a setting."""
    settings_registry.refresh_if_changed()
    return await call_next(request)


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


class ConversationCreate(BaseModel):
    agent_id: str
    title: str = ""
    channel: str = "dashboard"
    started_by: str = "owner"
    owner: str = ""


class MessageIn(BaseModel):
    text: str
    attachments: list[dict[str, Any]] = []


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


# --------------------------------------------------------------------------- chat / conversations
# One backend: the dashboard and the messaging channels call these same functions (chat.py, conversations.py)
# in-process; this is the HTTP surface for everything else (the CLI, external integrations, tests).


@api.get("/conversations")
def list_conversations(include_archived: bool = False, only_unread: bool = False, search: str = "",
                       limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    return conversations.list_conversations(include_archived=include_archived, only_unread=only_unread,
                                             search=search, limit=limit)


@api.post("/conversations")
def create_conversation(req: ConversationCreate) -> dict[str, Any]:
    if req.agent_id not in chat.chat_agents():
        raise HTTPException(400, f"agent_id must be one of {chat.chat_agents()}")
    conv_id = conversations.create(req.agent_id, req.owner, title=req.title, channel=req.channel,
                                   started_by=req.started_by)
    return conversations.get(conv_id)


@api.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict[str, Any]:
    if (conv := conversations.get(conversation_id)) is None:
        raise HTTPException(404, "conversation not found")
    return conv


@api.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, by: str = "") -> dict[str, Any]:
    if not conversations.delete(conversation_id, by=by):
        raise HTTPException(404, "conversation not found")
    return {"deleted": True}


@api.post("/conversations/{conversation_id}/messages")
def post_message(conversation_id: str, req: MessageIn) -> dict[str, Any]:
    """Runs one chat turn through ``chat.converse`` and returns the reply (no streaming).

    403s before calling ``chat.converse`` when chat is blocked (dashboard login off or unconfigured) — the same
    check ``converse`` itself documents, surfaced here so API clients get a clear reason instead of an opaque error.
    """
    if conversations.get(conversation_id) is None:
        raise HTTPException(404, "conversation not found")
    if (reason := chat.blocked_reason()) is not None:
        raise HTTPException(403, reason)
    turn = chat.converse(conversation_id, req.text, attachments=req.attachments)
    return asdict(turn)


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"


@api.get("/conversations/{conversation_id}/stream")
def stream_conversation(conversation_id: str, message_id: int,
                        user_attachment: str | None = Query(default=None)) -> StreamingResponse:
    """Streams one chat turn's events (text_delta, tool_start, tool_end, escalation) over SSE, then a final
    ``done`` event. ``message_id`` is the id of the user message (already in the conversation) to reply to.
    ``user_attachment`` is an optional single attachment (e.g. a ``data:`` URL) for that same turn."""
    conv = conversations.get(conversation_id)
    if conv is None:
        raise HTTPException(404, "conversation not found")
    msg = next((m for m in conv["messages"] if m["id"] == message_id), None)
    if msg is None or msg["role"] != "user":
        raise HTTPException(404, "message not found")
    if (reason := chat.blocked_reason()) is not None:
        raise HTTPException(403, reason)

    attachments = [{"url": user_attachment}] if user_attachment else []
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def on_event(evt: dict[str, Any]) -> None:
        events.put(evt)

    def run() -> None:
        try:
            turn = chat.converse(conversation_id, msg["content"], attachments=attachments, on_event=on_event)
        except Exception as exc:  # the stream must end cleanly even if the turn blew up
            events.put({"type": "error", "detail": str(exc)})
        else:
            events.put({"type": "done", "message_id": turn.message_id, "status": turn.status,
                       "cost_eur": turn.cost_eur, "escalations": turn.escalations})
        finally:
            events.put(None)

    threading.Thread(target=run, daemon=True).start()

    def gen() -> Iterator[str]:
        while (evt := events.get()) is not None:
            yield _sse(evt)

    return StreamingResponse(gen(), headers={"Content-Type": "text/event-stream"})


@api.get("/inbox")
def inbox() -> list[dict[str, Any]]:
    """Conversations an agent started that the owner has not seen yet (``started_by=agent``, ``unread>0``)."""
    return [c for c in conversations.list_conversations(only_unread=True, limit=200) if c.get("started_by") == "agent"]


@api.get("/status")
def status_route() -> dict[str, Any]:
    """Overview banner data: switches, safe mode, open incidents, pending approvals, unread messages, last backup."""
    from aecom import backup
    from aecom.incidents import open_counts

    last_backup: dict[str, Any] | None = None
    if backups := backup.list_backups():
        path = backups[0]
        try:
            manifest = backup.read_manifest(path)
            created = backup.created_at(path).isoformat()
            size_mb = round(sum(f.get("bytes", 0) for f in manifest.get("files", {}).values()) / 1_000_000, 3)
            last_backup = {"started_at": created, "completed_at": created, "size_mb": size_mb,
                           "success": not backup.check_hashes(path)}
        except backup.BackupError:
            last_backup = {"started_at": None, "completed_at": None, "size_mb": 0.0, "success": False}

    return {
        "kill_switch": kill_switch_on(),
        "agents_paused": agents_paused_on(),
        "safe_mode": safe_mode_on(),
        "open_incidents": open_counts(),
        "pending_approvals": len(approvals.list_approvals("pending")),
        "unread_messages": conversations.unread_total(),
        "last_backup": last_backup,
    }


app.include_router(api)
app.include_router(webhooks.router)  # signature-verified instead of the bearer token
