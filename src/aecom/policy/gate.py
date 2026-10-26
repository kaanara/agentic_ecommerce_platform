"""In-line execution gate (escalation matrix).

Every tool an agent can call is wrapped here. Order of checks:
  1. kill switch      → deny every side-effecting call
  2. agents paused    → deny side-effecting calls except ``defaults.continue_while_paused`` (order delivery)
  3. forbidden pattern → deny sign-ups, logins and form submission (T4), read-only or not
  4. allowlist        → deny tools the agent was not granted (done when tools are built)
  5. policy rule      → tier T0..T4, agent restriction, limits, price-change %, monthly budgets
  6. allow → execute; escalate → write an Approval and return immediately; deny → explain

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

from aecom.audit import audit
from aecom.company_settings import agents_paused_on, get_setting, kill_switch_on
from aecom.config import get_settings
from aecom.context import current_agent, current_counterparty, current_run, email_address
from aecom.db import Approval, Campaign, ToolCallLog, Vendor, now, session
from aecom.money import quantize, to_decimal

TIER_ORDER = ["T0", "T1", "T2", "T3", "T4"]


@dataclass
class GateResult:
    decision: str  # allow | deny | escalate
    tier: str
    reason: str


@lru_cache
def load_policy() -> dict[str, Any]:
    return yaml.safe_load(get_settings().policy_file.read_text())


def is_forbidden(tool_name: str, policy: dict[str, Any] | None = None) -> bool:
    """True if the tool name (or the bare tool part of ``mcp:<server>/<tool>``) hits a forbidden pattern."""
    patterns = (policy or load_policy())["defaults"].get("forbidden_patterns") or []
    names = {tool_name.lower(), tool_name.rsplit("/", 1)[-1].lower()}
    return any(fnmatch.fnmatchcase(n, p.lower()) for n in names for p in patterns)


def _max_tier(a: str, b: str) -> str:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


def _month_start() -> datetime:
    n = now()  # UTC, same clock as created_at
    return datetime(n.year, n.month, 1)


def _marketing_committed(args: dict[str, Any]) -> Decimal:
    """Marketing money committed this month if this launch goes through (I9).

    = spend this month of every campaign created this month or still live/paused (any status, so pausing
      does not hide it)
    + remaining budget (budget - spent) of the *other* currently live campaigns (last month's included)
    + the requested budget; on a relaunch only the part not already spent is new."""
    own_id, requested = args.get("campaign_id"), to_decimal(args.get("budget_eur", 0) or 0)
    spent = remaining = Decimal(0)
    own_spent = Decimal(0)
    with session() as s:
        rows = s.scalars(select(Campaign).where(or_(Campaign.created_at >= _month_start(),
                                                    Campaign.status.in_(["live", "paused"])))).all()
        for c in rows:
            spent += to_decimal(c.spent_eur or 0)
            if c.id == own_id:
                own_spent = to_decimal(c.spent_eur or 0)
            elif c.status == "live":
                remaining += max(to_decimal(c.budget_eur or 0) - to_decimal(c.spent_eur or 0), Decimal(0))
    return quantize(spent + remaining + max(requested - own_spent, Decimal(0)))


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


def evaluate(agent_id: str, tool_name: str, args: dict[str, Any], *, side_effect: bool = True,
             sandboxed: bool = False) -> GateResult:
    policy = load_policy()
    rule = policy["rules"].get(tool_name)
    if rule is None:
        tier = "T0" if not side_effect else policy["defaults"]["unknown_tool_tier"]
        rule = {"tier": tier, "reason": "Unclassified tool."}
    tier = rule["tier"]
    if sandboxed and side_effect:
        tier = _max_tier(tier, policy["defaults"]["sandboxed_tool_tier"])

    if side_effect and kill_switch_on():
        return GateResult("deny", tier, "Kill switch is ON — all side-effecting actions are blocked.")
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
    for arg, maximum in (rule.get("limits") or {}).items():
        if arg in args and float(args[arg]) > float(maximum):
            return GateResult("escalate", tier, f"{arg}={args[arg]} exceeds limit {maximum}. {rule.get('reason', '')}".strip())
    if (pct := rule.get("price_change_pct_max")) is not None and "sku" in args:
        from aecom.store.adapter import get_store

        current = (get_store().get_product(args["sku"]) or {}).get("price_eur")
        if current:
            change = abs(float(args["price_eur"]) - current) / current * 100
            if change > pct:
                return GateResult("escalate", tier, f"price change {change:.0f}% exceeds {pct}%. {rule.get('reason', '')}".strip())
    if key := rule.get("monthly_budget_setting"):
        budget = float(get_setting(key) or policy["company_settings_defaults"][key])
        committed = _committed(key, args)
        if committed > to_decimal(budget):
            return GateResult("escalate", tier, f"would bring {key} to €{committed:.0f} of €{budget:.0f}.")
    return GateResult("allow", tier, "within limits")


def dedup_key(agent_id: str, tool_name: str, args: dict[str, Any]) -> str:
    """Identical escalations (same agent, tool and args) share one pending approval (I10)."""
    payload = agent_id + "\x1f" + tool_name + "\x1f" + json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _to_text(result: Any) -> str:
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    return text if len(text) < 12000 else text[:12000] + " …[truncated]"


async def run_gated(agent_id: str, tool_name: str, args: dict[str, Any], execute: Callable[[dict[str, Any]], Any | Awaitable[Any]],
                    *, side_effect: bool, sandboxed: bool = False, context: str = "") -> str:
    """Evaluate policy, then execute / escalate / deny. Always returns a string for the model."""
    run_id = current_run.get()
    verdict = evaluate(agent_id, tool_name, args, side_effect=side_effect, sandboxed=sandboxed)
    with session() as s:
        s.add(ToolCallLog(run_id=run_id, agent_id=agent_id, tool_name=tool_name, decision=verdict.decision))
    if verdict.tier != "T0" or verdict.decision != "allow":
        audit(agent_id, tool_name, verdict.decision, {"args": args, "tier": verdict.tier, "reason": verdict.reason}, run_id)

    if verdict.decision == "deny":
        return f"DENIED by policy ({verdict.tier}): {verdict.reason}"
    if verdict.decision == "escalate":
        key = dedup_key(agent_id, tool_name, args)
        ttl = float(load_policy()["defaults"].get("approval_ttl_hours", 72))
        with session() as s:
            existing = s.scalars(select(Approval).where(Approval.dedup_key == key, Approval.status == "pending",
                                                        or_(Approval.expires_at.is_(None), Approval.expires_at > now()))
                                 .order_by(Approval.created_at)).first()
            if existing is not None:
                return (f"ESCALATED to human approval ({verdict.tier}, id={existing.id}): {verdict.reason} "
                        "This exact request is already pending - do not retry it; continue with the rest of your task "
                        "and mention the pending approval in your final answer.")
            a = Approval(agent_id=agent_id, tool_name=tool_name, args=args, risk_tier=verdict.tier,
                         reason=verdict.reason, context=context[:4000], run_id=run_id, dedup_key=key,
                         expires_at=now() + timedelta(hours=ttl))
            s.add(a)
            s.flush()
            approval_id = a.id
        return (f"ESCALATED to human approval ({verdict.tier}, id={approval_id}): {verdict.reason} "
                "It will execute exactly as requested if approved. Do not retry it; continue with the rest of your task "
                "and mention the pending approval in your final answer.")
    try:
        result = execute(args)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # tool errors go back to the model so it can recover
        with session() as s:
            s.add(ToolCallLog(run_id=run_id, agent_id=agent_id, tool_name=tool_name, decision="error", ok=False))
        return f"ERROR: {type(exc).__name__}: {exc}"
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
