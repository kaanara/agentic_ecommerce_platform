"""Per-call context (current run / agent) without threading it through every function signature."""

from __future__ import annotations

from contextvars import ContextVar

current_run: ContextVar[str | None] = ContextVar("current_run", default=None)
current_agent: ContextVar[str] = ContextVar("current_agent", default="system")
