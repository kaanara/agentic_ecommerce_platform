"""Per-call context (current run / agent) without threading it through every function signature."""

from __future__ import annotations

from contextvars import ContextVar

current_run: ContextVar[str | None] = ContextVar("current_run", default=None)
current_agent: ContextVar[str] = ContextVar("current_agent", default="system")
# The customer address an agent is serving in this run (the sender of the inbound email). Agents may only
# email this address without a human; see `recipient_must_be_counterparty` in policy.yaml.
current_counterparty: ContextVar[str | None] = ContextVar("current_counterparty", default=None)


def email_address(value: str) -> str:
    """'Name <a@b.c>' → 'a@b.c' (lower-case); '' if there is no address."""
    from email.utils import parseaddr

    return parseaddr(value or "")[1].strip().lower()
