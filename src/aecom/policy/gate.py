"""In-line execution gate (escalation matrix).

Every tool an agent can call is wrapped here. Order of checks:
  1. kill switch      → deny every side-effecting call
  2. safe mode       → deny side-effecting calls except ``defaults.continue_in_safe_mode`` (deliveries, replies)
  3. agents paused    → deny side-effecting calls except ``defaults.continue_while_paused`` (order delivery)
  4. forbidden pattern → deny sign-ups, logins and form submission (T4), read-only or not
  5. allowlist        → deny tools the agent was not granted (done when tools are built)
  6. policy rule      → tier T0..T4, agent restriction, limits, price-change %, monthly budgets. For
                        ``mcp:<server>/<tool>`` (``resolve_rule``, F5): exact tool rule > server wildcard
                        (``mcp:<server>/*``) > ``defaults.mcp_unclassified_tier`` - always escalates to a human
                        unless a human already classified the tool or server. A destructive/financial-looking name
                        on an otherwise unclassified MCP tool (``looks_destructive``) is bumped to at least T3.
  7. allow → execute; escalate → write an Approval and return immediately; deny → explain

Escalations do *not* pause the graph. The exact call (tool + args) is parked in ``ops_approvals`` and
executed verbatim when a human approves it — the human approves what will run, not a later re-generation.
An identical call (same agent, tool, args) that is still pending is not parked twice, and approvals expire after
``defaults.approval_ttl_hours``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Awaitable, Callable

import yaml
from langchain_core.tools import BaseTool, StructuredTool
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from aecom.audit import audit, mask_detail
from aecom.company_settings import agents_paused_on, get_setting, kill_switch_on, safe_mode_on
from aecom.config import get_settings
from aecom.context import current_agent, current_counterparty, current_run, email_address
from aecom.db import Approval, Campaign, LedgerEntry, ToolCallLog, Vendor, now, session
from aecom.money import quantize, to_decimal

TIER_ORDER = ["T0", "T1", "T2", "T3", "T4"]
SAFE_MODE_REASON = ("Safe mode: an unacknowledged P1 incident - only deliveries and customer replies continue; "
                    "no spending.")


@dataclass
class GateResult:
    decision: str  # allow | deny | escalate
    tier: str
    reason: str
    autonomy_stage: int = 1  # Phase 5: stage of the action when it was judged (T2 only)
    autonomous: bool = False  # allowed only thanks to a raised stage (the stage-1 rule would have escalated)


def _notify_owner(event: dict) -> None:
    try:
        from aecom import notify

        notify.route(event)
    except Exception:  # noqa: BLE001 - a notification problem must never block or fail a gate decision
        pass


@lru_cache
def load_policy() -> dict[str, Any]:
    """The active policy: the UI-saved version if one is active (``aecom.policy.versions``), else the file shipped
    in git. Cached with no args; busted by ``aecom.policy.versions`` whenever a version is saved or activated."""
    from aecom.policy.versions import active_yaml

    text = active_yaml() or get_settings().policy_file.read_text()
    return yaml.safe_load(text)


def is_forbidden(tool_name: str, policy: dict[str, Any] | None = None) -> bool:
    """True if the tool name hits a forbidden pattern. For ``mcp:<server>/<tool>`` only the tool part is matched (R4),
    so a server called e.g. ``login_service`` does not deny its read tools; builtin names are matched whole."""
    patterns = (policy or load_policy())["defaults"].get("forbidden_patterns") or []
    name = tool_name.lower()
    if name.startswith("mcp:"):
        name = name.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(name, p.lower()) for p in patterns)


# F5: a name fragment that looks destructive or financial. Checked only for an *unclassified* MCP tool (no exact or
# wildcard rule) - a human never got to look at it, so a name like this is treated as at least T3 rather than
# whatever `defaults.mcp_unclassified_tier` says, and the bump shows up in the reason (and so in the audit log via
# the normal "non-T0 / non-allow" logging in `run_gated`).
MCP_DESTRUCTIVE_NAME_HINTS = ("create_refund", "delete", "send", "pay", "transfer", "charge", "submit", "purchase",
                             "login", "sign_in", "download")


def looks_destructive(tool_name: str) -> bool:
    """True if the bare tool part of ``tool_name`` (after the last ``/`` for ``mcp:<server>/<tool>``) contains one of
    ``MCP_DESTRUCTIVE_NAME_HINTS`` as a substring, case-insensitive."""
    name = tool_name.rsplit("/", 1)[-1].lower() if tool_name.startswith("mcp:") else tool_name.lower()
    return any(hint in name for hint in MCP_DESTRUCTIVE_NAME_HINTS)


def resolve_rule(tool_name: str, policy: dict[str, Any], *, side_effect: bool) -> dict[str, Any]:
    """The policy rule that applies to ``tool_name`` (F5).

    For ``mcp:<server>/<tool>``: an exact rule for the tool > a server wildcard (``mcp:<server>/*``) >
    ``defaults.mcp_unclassified_tier`` (falls back to ``defaults.unknown_tool_tier`` for a policy saved before F5).
    A read-only call (``side_effect=False``) with no rule at all stays T0, same as an unclassified builtin tool.
    A destructive/financial-looking name (``looks_destructive``) on an otherwise unclassified MCP tool is bumped to
    at least T3, whatever the unclassified default would have said.

    For any other tool name: the exact rule, or T0 (read-only) / ``defaults.unknown_tool_tier`` (side-effecting).
    """
    rules = policy["rules"]
    if tool_name in rules:
        return rules[tool_name]
    if tool_name.startswith("mcp:"):
        server = tool_name[len("mcp:"):].split("/", 1)[0]
        wildcard = rules.get(f"mcp:{server}/*")
        if wildcard is not None:
            return wildcard
        destructive = looks_destructive(tool_name)
        tier = "T0" if not side_effect else policy["defaults"].get("mcp_unclassified_tier",
                                                                   policy["defaults"]["unknown_tool_tier"])
        if destructive:
            tier = _max_tier(tier, "T3")
            return {"tier": tier, "reason": f"Unclassified MCP tool '{tool_name}' looks destructive or financial "
                                            "by name; treated as at least T3 until a human classifies it."}
        return {"tier": tier, "reason": "Unclassified MCP tool."}
    tier = "T0" if not side_effect else policy["defaults"]["unknown_tool_tier"]
    return {"tier": tier, "reason": "Unclassified tool."}


def _max_tier(a: str, b: str) -> str:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


def _month_start() -> datetime:
    n = now()  # UTC, same clock as created_at
    return datetime(n.year, n.month, 1)


def _marketing_committed(args: dict[str, Any]) -> Decimal:
    """Marketing money committed this calendar month if this launch goes through (I9, R9).

    = marketing spend *booked this month* (ledger ``marketing_expense`` lines created since the UTC month start, so a
      campaign spanning months only counts what it spent this month; paused/ended campaigns still count)
    + remaining budget (budget - spent) of the *other* currently live campaigns
    + the requested budget; on a relaunch only the part not already spent (lifetime) is new."""
    own_id, requested = args.get("campaign_id"), to_decimal(args.get("budget_eur", 0) or 0)
    remaining = own_spent = Decimal(0)
    with session() as s:
        booked = s.scalar(select(func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0))
                          .where(LedgerEntry.account == "marketing_expense", LedgerEntry.created_at >= _month_start())) or 0
        for c in s.scalars(select(Campaign).where(Campaign.status == "live")):
            if c.id == own_id:
                continue
            remaining += max(to_decimal(c.budget_eur or 0) - to_decimal(c.spent_eur or 0), Decimal(0))
        if own_id and (own := s.get(Campaign, own_id)) is not None:
            own_spent = to_decimal(own.spent_eur or 0)
    return quantize(to_decimal(booked) + remaining + max(requested - own_spent, Decimal(0)))


def _committed(setting_key: str, args: dict[str, Any]) -> Decimal:
    """Total for ``setting_key`` this month including the requested amount."""
    if setting_key == "marketing_budget_month_eur":
        return _marketing_committed(args)
    amount = to_decimal(args.get("budget_eur", args.get("monthly_cost_eur", 0)) or 0)
    with session() as s:
        if setting_key == "vendor_budget_month_eur":
            current = s.scalar(select(func.coalesce(func.sum(Vendor.monthly_cost_eur), 0))
                               .where(Vendor.status.in_(["active", "provisioning", "evaluating"]))) or 0.0
            return quantize(to_decimal(current) + amount)
    return quantize(amount)


def _t2_escalation(rule: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Per-call limit / price-change check of a T2 rule (base or stage-effective). Returns the escalation reason or None."""
    for arg, maximum in (rule.get("limits") or {}).items():
        if arg in args and float(args[arg]) > float(maximum):
            return f"{arg}={args[arg]} exceeds limit {maximum}. {rule.get('reason', '')}".strip()
    if (pct := rule.get("price_change_pct_max")) is not None and "sku" in args:
        from aecom.store.adapter import get_store

        current = (get_store().get_product(args["sku"]) or {}).get("price_eur")
        if current:
            change = abs(float(args["price_eur"]) - current) / current * 100
            if change > pct:
                return f"price change {change:.0f}% exceeds {pct}%. {rule.get('reason', '')}".strip()
    return None


def evaluate(agent_id: str, tool_name: str, args: dict[str, Any], *, side_effect: bool = True,
             sandboxed: bool = False) -> GateResult:
    policy = load_policy()
    rule = resolve_rule(tool_name, policy, side_effect=side_effect)
    tier = rule["tier"]
    if sandboxed and side_effect:
        tier = _max_tier(tier, policy["defaults"]["sandboxed_tool_tier"])

    if side_effect and kill_switch_on():
        return GateResult("deny", tier, "Kill switch is ON — all side-effecting actions are blocked.")
    if side_effect and safe_mode_on() and tool_name not in (policy["defaults"].get("continue_in_safe_mode") or []):
        return GateResult("deny", tier, SAFE_MODE_REASON)
    if side_effect and agents_paused_on() and tool_name not in (policy["defaults"].get("continue_while_paused") or []):
        return GateResult("deny", tier, "Agents are paused - only order delivery continues.")
    if is_forbidden(tool_name, policy):
        return GateResult("deny", "T4", "Forbidden for agents: accounts, logins and form submission are human-only.")
    if "agents" in rule and agent_id not in rule["agents"]:
        return GateResult("deny", tier, f"{tool_name} is reserved for {', '.join(rule['agents'])}.")
    if tier == "T4":
        return GateResult("deny", tier, "Forbidden for agents.")
    if tier == "T3":
        return GateResult("escalate", tier, rule.get("reason", "High-stakes action requires a human."))
    if tier in ("T0", "T1"):
        return GateResult("allow", tier, "within policy")

    # T2 — bounded risk
    if rule.get("recipient_must_be_counterparty"):
        to, counterparty = email_address(str(args.get("to", ""))), current_counterparty.get()
        if not counterparty or to != counterparty:
            whom = f"the customer of this case ({counterparty})" if counterparty else "no customer case in this run"
            return GateResult("escalate", tier, f"recipient {to or '?'} is not {whom}. {rule.get('reason', '')}".strip())
    from aecom.autonomy import current_stage, effective_rule

    stage = current_stage(tool_name)
    over = _t2_escalation(effective_rule(tool_name, rule, stage), args)
    if over:
        return GateResult("escalate", tier, over, stage)
    if key := rule.get("monthly_budget_setting"):
        budget = float(get_setting(key) or policy["company_settings_defaults"][key])
        committed = _committed(key, args)
        if committed > to_decimal(budget):
            return GateResult("escalate", tier, f"would bring {key} to €{committed:.0f} of €{budget:.0f}.", stage)
    # autonomous = this call only passes because of the earned stage; the base rule would have escalated it
    autonomous = stage > 1 and _t2_escalation(rule, args) is not None
    return GateResult("allow", tier, "within limits", stage, autonomous)


def dedup_key(agent_id: str, tool_name: str, args: dict[str, Any]) -> str:
    """Identical escalations (same agent, tool and args) share one pending approval (I10)."""
    payload = agent_id + "\x1f" + tool_name + "\x1f" + json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _pending_row(s: Any, key: str) -> Approval | None:
    return s.scalars(select(Approval).where(Approval.dedup_key == key, Approval.status == "pending",
                                            or_(Approval.expires_at.is_(None), Approval.expires_at > now()))
                     .order_by(Approval.created_at)).first()


def _to_text(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return text if len(text) < 12000 else text[:12000] + " …[truncated]"


SUMMARY_MAX = 500


def summarize(value: Any, limit: int = SUMMARY_MAX) -> str:
    """Shortened, masked, JSON-ish copy of tool args / results for ``ops_tool_calls`` (D3). Secret shapes are masked,
    licence keys, download links, tokens and credentials redacted (also inside a JSON string result)."""
    from aecom.observability import mask

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        value = parsed if isinstance(parsed, (dict, list)) else value
    masked = mask_detail(value)
    text = masked if isinstance(masked, str) else json.dumps(masked, default=str, ensure_ascii=False)
    text = mask(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _log_call(run_id: str | None, agent_id: str, tool_name: str, decision: str, args: dict[str, Any],
              ok: bool = True, result: Any = None) -> int:
    with session() as s:
        row = ToolCallLog(run_id=run_id, agent_id=agent_id, tool_name=tool_name, decision=decision, ok=ok,
                          args_summary=summarize(args), result_summary=None if result is None else summarize(result))
        s.add(row)
        s.flush()
        return row.id


def _log_result(call_id: int, result: Any) -> None:
    with session() as s:
        if (row := s.get(ToolCallLog, call_id)) is not None:
            row.result_summary = summarize(result)


async def run_gated(agent_id: str, tool_name: str, args: dict[str, Any], execute: Callable[[dict[str, Any]], Any | Awaitable[Any]],
                    *, side_effect: bool, sandboxed: bool = False, context: str = "") -> str:
    """Evaluate policy, then execute / escalate / deny. Always returns a string for the model."""
    run_id = current_run.get()
    verdict = evaluate(agent_id, tool_name, args, side_effect=side_effect, sandboxed=sandboxed)
    call_id = _log_call(run_id, agent_id, tool_name, verdict.decision, args)  # D3: before executing, result added below
    if verdict.tier != "T0" or verdict.decision != "allow":
        detail = {"args": args, "tier": verdict.tier, "reason": verdict.reason}
        if verdict.decision == "allow":
            detail |= {"autonomy_stage": verdict.autonomy_stage, "autonomous": verdict.autonomous}
        audit(agent_id, tool_name, verdict.decision, detail, run_id)

    if verdict.decision == "deny":
        message = f"DENIED by policy ({verdict.tier}): {verdict.reason}"
        _log_result(call_id, message)
        return message
    if verdict.decision == "escalate":
        key = dedup_key(agent_id, tool_name, args)
        ttl = float(load_policy()["defaults"].get("approval_ttl_hours", 72))
        def already(a_id: str) -> str:
            return (f"ESCALATED to human approval ({verdict.tier}, id={a_id}): {verdict.reason} "
                    "This exact request is already pending - do not retry it; continue with the rest of your task "
                    "and mention the pending approval in your final answer.")

        new_approval_id = None
        with session() as s:
            existing = _pending_row(s, key)
            if existing is not None:
                message = already(existing.id)
            else:
                a = Approval(agent_id=agent_id, tool_name=tool_name, args=args, risk_tier=verdict.tier,
                             reason=verdict.reason, context=context[:4000], run_id=run_id, dedup_key=key,
                             expires_at=now() + timedelta(hours=ttl))
                try:
                    with s.begin_nested():  # R8: a racing identical escalation hits the unique pending index
                        s.add(a)
                        s.flush()
                    new_approval_id = a.id
                    message = (f"ESCALATED to human approval ({verdict.tier}, id={a.id}): {verdict.reason} "
                               "It will execute exactly as requested if approved. Do not retry it; continue with the rest of "
                               "your task and mention the pending approval in your final answer.")
                except IntegrityError:
                    winner = _pending_row(s, key)
                    if winner is None:
                        raise
                    message = already(winner.id)
        if new_approval_id:
            _notify_owner({"kind": "approval", "text": f"{agent_id} needs approval ({verdict.tier}) for {tool_name}",
                           "urgency": "normal", "approval_id": new_approval_id, "agent_id": agent_id})
        _log_result(call_id, message)
        return message
    try:
        result = execute(args)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # tool errors go back to the model so it can recover
        from aecom.observability import mask, record_error

        record_error("gate", exc, run_id=run_id, context={"agent_id": agent_id, "tool": tool_name})
        message = mask(f"ERROR: {type(exc).__name__}: {exc}")
        _log_result(call_id, message)
        _log_call(run_id, agent_id, tool_name, "error", args, ok=False, result=message)
        return message
    _log_result(call_id, result)
    return _to_text(result)


def gate_builtin(name: str, agent_id: str, context: str = "") -> BaseTool:
    """Turn a catalog function into a gated LangChain tool for one agent."""
    from aecom.tools.catalog import CATALOG

    td = CATALOG[name]
    base = StructuredTool.from_function(td.fn, name=name, description=inspect.getdoc(td.fn) or name)

    def execute(args: dict[str, Any]) -> Any:
        token = current_agent.set(agent_id)
        try:
            return td.fn(**args)
        finally:
            current_agent.reset(token)

    async def _call(**kwargs: Any) -> str:
        return await run_gated(agent_id, name, kwargs, execute, side_effect=td.side_effect, context=context)

    return StructuredTool(name=name, description=base.description, args_schema=base.args_schema, coroutine=_call,
                          metadata={"terminal": td.terminal, "side_effect": td.side_effect})


def gate_external(tool: BaseTool, agent_id: str, qualified_name: str, *, side_effect: bool, sandboxed: bool) -> BaseTool:
    """Wrap a tool loaded from an MCP server."""

    async def execute(args: dict[str, Any]) -> Any:
        return await tool.ainvoke(args)

    async def _call(**kwargs: Any) -> str:
        return await run_gated(agent_id, qualified_name, kwargs, execute, side_effect=side_effect, sandboxed=sandboxed)

    return StructuredTool(name=tool.name, description=f"[MCP {qualified_name}] {tool.description}",
                          args_schema=tool.args_schema, coroutine=_call,
                          metadata={"terminal": False, "side_effect": side_effect, "mcp": qualified_name})
