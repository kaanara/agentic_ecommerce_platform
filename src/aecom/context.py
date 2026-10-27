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


# I15: True once untrusted content (email, web page, inbox listing) has been fenced in this run. `remember` stores
# it on the memory, so a fact written after reading an attacker's text carries that provenance. `catalog.fence`
# sets it; `run_agent` calls `reset_taint(task, context)` when a run starts so the flag never leaks from one run into
# the next, while a task that itself carries fenced content (e.g. the customer email handed to Support) starts tainted.
# Tool calls may run in a copied context (asyncio tasks), where a ContextVar.set is not seen by later calls, so the
# (run id, agent) is remembered as well: the agent's run stays tainted for every later tool call of that run.
UNTRUSTED_MARK = "<untrusted source="  # the opening tag `catalog.fence` writes
current_tainted: ContextVar[bool] = ContextVar("current_tainted", default=False)
_tainted_runs: set[tuple[str, str]] = set()


def mark_tainted() -> None:
    current_tainted.set(True)
    if run := current_run.get():
        _tainted_runs.add((run, current_agent.get()))


def is_tainted() -> bool:
    return current_tainted.get() or (current_run.get() or "", current_agent.get()) in _tainted_runs


def reset_taint(*inputs: str) -> None:
    """Start a run clean; tainted from the outset if any input (task, context) already holds fenced content."""
    current_tainted.set(False)
    _tainted_runs.discard((current_run.get() or "", current_agent.get()))
    if any(UNTRUSTED_MARK in (x or "") for x in inputs):
        mark_tainted()
