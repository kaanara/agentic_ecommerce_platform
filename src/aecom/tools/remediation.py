"""Remediation tools for the Sentinel (K5, K9), registered in the same CATALOG as the built-ins.

Every one is side-effecting, so the policy gate checks it like any other agent's tool (kill switch, safe mode, agent
pause, the ``policy.yaml`` rule reserving it for the Sentinel). They only do what a runbook already lists as a safe
action for a human: replay a dead letter, re-run the reconciler, resend a missing delivery email, pause agents.
Undoing a pause, leaving safe mode, the kill switch, acknowledging or resolving incidents stay human.

Data fixes never edit history (K9): ``propose_correcting_entry`` is T3, so the gate only parks it as an approval;
the body runs when a human approves it and appends a new balanced entry with :func:`aecom.ledger.post`.

Two rules the gate can't express are enforced here, with the same approval mechanism (``ops_approvals``):
a dead letter outside ``policy.yaml → rules.replay_dead_letter.topic_prefixes`` is escalated to a human, and the
correcting entry is posted only while its approval is being executed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select

from aecom.audit import audit
from aecom.context import current_agent, current_run
from aecom.db import Approval, EventRow, now, session
from aecom.tools.catalog import tool

AGENT = "sentinel"
DEFAULT_TOPIC_PREFIXES = ("order.", "inventory.", "email.")
CORRECTION_PREFIX = "correction:"


def _topic_prefixes() -> tuple[str, ...]:
    from aecom.policy.gate import load_policy

    rule = load_policy()["rules"].get("replay_dead_letter") or {}
    return tuple(rule.get("topic_prefixes") or DEFAULT_TOPIC_PREFIXES)


def _same_args(stored: dict[str, Any], args: dict[str, Any]) -> bool:
    return all(stored.get(k) == v for k, v in args.items())


def _being_approved(tool_name: str, args: dict[str, Any]) -> bool:
    """True while a human's approval of exactly this call executes (approvals.approve marks it 'approved' first and
    writes the result only afterwards). An agent can't create such a row, so this can't be faked by a tool call."""
    with session() as s:
        rows = s.scalars(select(Approval).where(Approval.tool_name == tool_name, Approval.status == "approved",
                                                Approval.result.is_(None))).all()
        return any(_same_args(dict(a.args or {}), args) for a in rows)


def _escalate(tool_name: str, args: dict[str, Any], tier: str, reason: str) -> str:
    """Park the exact call for a human, like the gate does (deduplicated, with the policy's TTL)."""
    from aecom.policy.gate import dedup_key, load_policy

    agent_id, run_id = current_agent.get() or AGENT, current_run.get()
    key = dedup_key(agent_id, tool_name, args)
    ttl = float(load_policy()["defaults"].get("approval_ttl_hours", 72))
    with session() as s:
        existing = s.scalars(select(Approval).where(Approval.dedup_key == key, Approval.status == "pending",
                                                    or_(Approval.expires_at.is_(None), Approval.expires_at > now()))
                             ).first()
        if existing is not None:
            approval_id, new = existing.id, False
        else:
            a = Approval(agent_id=agent_id, tool_name=tool_name, args=args, risk_tier=tier, reason=reason,
                         run_id=run_id, dedup_key=key, expires_at=now() + timedelta(hours=ttl))
            s.add(a)
            s.flush()
            approval_id, new = a.id, True
    if new:
        audit(agent_id, tool_name, "escalate", {"args": args, "tier": tier, "reason": reason}, run_id)
    return (f"ESCALATED to human approval ({tier}, id={approval_id}): {reason} It will execute exactly as requested "
            "if approved. Do not retry it; mention the pending approval in your diagnosis' next actions.")


@tool("ops", side_effect=True)
def replay_dead_letter(event_id: int) -> dict | str:
    """Re-queue ONE dead-lettered event and drain the bus (= `aecom events retry <ID>`). Only after its cause is fixed.
    order.* / inventory.* / email.* events replay at once; any other topic is escalated to a human."""
    from aecom.bus.events import drain, retry

    with session() as s:
        e = s.get(EventRow, int(event_id))
        if e is None or not e.dead:
            raise ValueError(f"event {event_id} is not in the dead letter")
        topic = e.topic
    args = {"event_id": event_id}
    if not topic.startswith(_topic_prefixes()) and not _being_approved("replay_dead_letter", args):
        return _escalate("replay_dead_letter", args, "T2",
                         f"Replaying a {topic} event is outside the Sentinel's routine topics; a human decides.")
    requeued = retry([int(event_id)])
    drained = drain()
    with session() as s:
        e = s.get(EventRow, int(event_id))
        state = "processed" if e.processed else "dead_letter" if e.dead else "waiting_for_retry"
        last_error = e.last_error
    return {"event_id": event_id, "topic": topic, "requeued": requeued, "drained": drained, "state": state,
            "last_error": (last_error or "")[:300]}


@tool("ops", side_effect=True)
def rerun_reconciler() -> dict:
    """Run the order reconciler once and drain the bus (= `aecom reconcile`): re-publishes order.paid for paid orders
    left undelivered or unemailed. Idempotent."""
    from aecom.bus.events import drain
    from aecom.reconciler import reconcile

    counts = reconcile()
    return {**counts, "drained": drain()}


@tool("ops", side_effect=True)
def resend_delivery_email(order_id: str) -> dict:
    """Resend the access email of a DELIVERED order whose email never went out (email_sent false), via the normal
    delivery path (no new key). Refused for any other order."""
    from aecom.store.adapter import get_store
    from aecom.tools.catalog import deliver_order

    order = get_store().get_order(order_id)
    if order is None:
        raise ValueError(f"order {order_id} not found")
    if order.get("status") != "delivered":
        raise ValueError(f"order {order_id} is '{order.get('status')}', not delivered - use the reconciler instead")
    if order.get("email_sent"):
        raise ValueError(f"order {order_id} already got its email; it is never sent twice")
    return deliver_order(order_id)


@tool("ops", side_effect=True)
def pause_agents(reason: str) -> dict:
    """Containment: pause all agents (deliveries keep running). Only a human unpauses. Give the reason (incident id
    and why), it is audited."""
    from aecom.company_settings import set_flag

    set_flag("agents_paused", True, by=AGENT)
    audit(AGENT, "pause_agents", "on", {"reason": reason}, current_run.get())
    return {"agents_paused": True, "reason": reason, "undo": "a human: uv run aecom agents-pause off"}


def _correction_lines(lines: list[list]) -> list[tuple[str, float, float]]:
    from aecom.db import LedgerEntry
    from aecom.ledger import DEBIT_NORMAL
    from aecom.money import cents

    with session() as s:
        known = DEBIT_NORMAL | {"revenue", "vat_payable"} | set(s.scalars(select(LedgerEntry.account).distinct()))
    if not lines or len(lines) < 2:
        raise ValueError("a correcting entry needs at least two lines")
    out: list[tuple[str, float, float]] = []
    for line in lines:
        if len(line) != 3:
            raise ValueError(f"line {line!r} is not [account, debit, credit]")
        account, debit, credit = str(line[0]).strip(), float(line[1] or 0), float(line[2] or 0)
        if not account or debit < 0 or credit < 0 or (debit and credit) or not (debit or credit):
            raise ValueError(f"line {line!r}: one account and exactly one positive amount (debit or credit)")
        if account not in known:
            raise ValueError(f"unknown ledger account {account!r}")
        out.append((account, debit, credit))
    if sum(cents(d) for _, d, _ in out) != sum(cents(c) for _, _, c in out):
        raise ValueError("unbalanced correcting entry: debits and credits must match to the cent")
    return out


@tool("ops", side_effect=True)
def propose_correcting_entry(lines: list[list], memo: str, ref: str | None = None) -> dict:
    """Propose a NEW correcting ledger entry for a data problem (never an edit of existing rows). Always waits for a
    human (T3); when approved it is posted as given. lines = [[account, debit, credit], ...], balanced to the cent;
    memo says what it corrects; ref = the order/txn it corrects."""
    if not _being_approved("propose_correcting_entry", {"lines": lines, "memo": memo, "ref": ref}):
        raise PermissionError("a correcting entry is posted only when a human approves it")
    from aecom import ledger

    checked = _correction_lines(lines)
    text = memo if memo.startswith(CORRECTION_PREFIX) else f"{CORRECTION_PREFIX} {memo}"
    txn = ledger.post(checked, text, ref)
    return {"txn_id": txn, "memo": text, "ref": ref, "lines": [list(x) for x in checked]}
