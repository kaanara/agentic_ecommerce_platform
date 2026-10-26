"""In-line execution gate (escalation matrix).

Every tool an agent can call is wrapped here. Order of checks:
  1. kill switch      → deny every side-effecting call
  2. allowlist        → deny tools the agent was not granted (done when tools are built)
  3. policy rule      → tier T0..T4, agent restriction, limits, price-change %, monthly budgets
  4. allow → execute; escalate → write an Approval and return immediately; deny → explain

Escalations do *not* pause the graph. The exact call (tool + args) is parked in ``ops_approvals`` and
executed verbatim when a human approves it — the human approves what will run, not a later re-generation.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any, Awaitable, Callable

import yaml
from langchain_core.tools import BaseTool, StructuredTool
from sqlalchemy import func, select

from aecom.audit import audit
from aecom.company_settings import get_setting, kill_switch_on
from aecom.config import get_settings
from aecom.context import current_agent, current_counterparty, current_run, email_address
from aecom.db import Approval, Campaign, ToolCallLog, Vendor, session

TIER_ORDER = ["T0", "T1", "T2", "T3", "T4"]


@dataclass
class GateResult:
    decision: str  # allow | deny | escalate
    tier: str
    reason: str


@lru_cache
def load_policy() -> dict[str, Any]:
    return yaml.safe_load(get_settings().policy_file.read_text())


def _max_tier(a: str, b: str) -> str:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


def _month_start() -> datetime:
    n = datetime.now()
    return datetime(n.year, n.month, 1)


def _monthly_spend(setting_key: str) -> float:
    with session() as s:
        if setting_key == "marketing_budget_month_eur":
            return s.scalar(select(func.coalesce(func.sum(Campaign.budget_eur), 0))
                            .where(Campaign.status.in_(["live", "ended"]), Campaign.created_at >= _month_start())) or 0.0
        if setting_key == "vendor_budget_month_eur":
            return s.scalar(select(func.coalesce(func.sum(Vendor.monthly_cost_eur), 0))
                            .where(Vendor.status.in_(["active", "provisioning", "evaluating"]))) or 0.0
    return 0.0


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
        amount = float(args.get("budget_eur", args.get("monthly_cost_eur", 0)))
        spent = _monthly_spend(key)
        if spent + amount > budget:
            return GateResult("escalate", tier, f"would bring {key} to €{spent + amount:.0f} of €{budget:.0f}.")
    return GateResult("allow", tier, "within limits")


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
        with session() as s:
            a = Approval(agent_id=agent_id, tool_name=tool_name, args=args, risk_tier=verdict.tier,
                         reason=verdict.reason, context=context[:4000], run_id=run_id)
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
