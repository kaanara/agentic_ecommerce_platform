"""Runs one agent on one task: persona prompt + gated tools + model loop.

Kept as an explicit loop (rather than a prebuilt ReAct agent) so the budget check, cost accounting,
terminal "submit_*" tools and per-step audit are visible and easy to change.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from sqlalchemy import func, select

from aecom import ledger
from aecom.audit import audit
from aecom.company_settings import agent_enabled, get_setting
from aecom.config import get_settings
from aecom.context import current_agent, current_run, reset_taint
from aecom.db import AgentRun, Lesson, Setting, new_id, now, session
from aecom.llm import get_model, server_tool_specs, server_tool_usage
from aecom.money import CENT, quantize, to_decimal
from aecom.observability import mask, record_error
from aecom.persona.loader import load_spec
from aecom.persona.prompt import system_prompt
from aecom.policy.gate import load_policy, run_gated
from aecom.registry.tools import build_tools, skills_for


@dataclass
class AgentResult:
    agent_id: str
    output: str
    status: str = "ok"  # ok | error | budget_exceeded | max_steps | disabled
    structured: dict[str, Any] | None = None
    tool_calls: int = 0
    cost_eur: float = 0.0
    escalations: list[str] = field(default_factory=list)
    web_searches: int = 0
    web_fetches: int = 0
    # One entry per model turn: {"role": "assistant", "content": str, "tool_steps": [{"name", "args", "decision",
    # "result", "terminal", "approval_id"?}, ...]}. Populated for every run (chat and automated); chat.py uses it to
    # fill ChatTurn.tool_steps and consult_agent nests it in its own result so depth is checkable.
    messages: list[dict[str, Any]] = field(default_factory=list)


def _text(msg: AIMessage) -> str:
    if isinstance(msg.content, str):
        return msg.content
    return "\n".join(b.get("text", "") for b in msg.content if isinstance(b, dict) and b.get("type") == "text")


def spent_today(agent_id: str) -> float:
    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with session() as s:
        return s.scalar(select(func.coalesce(func.sum(AgentRun.cost_eur), 0))
                        .where(AgentRun.agent_id == agent_id, AgentRun.started_at >= start)) or 0.0


def daily_budget(agent_id: str) -> float:
    spec = load_spec(agent_id)
    return float(spec.budget_eur_per_day or load_policy()["budgets"].get(agent_id, 2))


AI_COST_CARRY = "ai_cost_carry_eur"
CARRY_PRECISION = Decimal("0.00000001")


def book_ai_costs() -> str | None:
    """Book unbooked LLM run costs as one ai_expense entry once they add up to at least a cent.

    A single run often costs < €0.01, which would round to zero in the cents ledger. The rounding remainder
    (positive or negative) is carried in the company setting ``ai_cost_carry_eur`` so nothing is lost over time.
    """
    with session() as s:
        runs = list(s.execute(select(AgentRun.id, AgentRun.cost_eur)
                              .where(AgentRun.cost_booked.is_not(True), AgentRun.cost_eur > 0)))
    carry = to_decimal(get_setting(AI_COST_CARRY, "0") or "0")
    total = carry + sum((to_decimal(c) for _, c in runs), to_decimal(0))
    if not runs or total < CENT:
        return None
    amount = quantize(total)
    # one transaction: ledger line + cost_booked flags + carry either all commit or none do
    with session() as s:
        txn = ledger.post([("ai_expense", amount, 0), ("cash", 0, amount)], f"LLM usage ({len(runs)} runs)",
                          runs[-1][0], session=s)
        for row in s.scalars(select(AgentRun).where(AgentRun.id.in_([r for r, _ in runs]))):
            row.cost_booked = True
        value = str((total - amount).quantize(CARRY_PRECISION))
        setting = s.get(Setting, AI_COST_CARRY)
        if setting:
            setting.value = value
        else:
            s.add(Setting(key=AI_COST_CARRY, value=value))
    return txn


def _book_ai_costs_safe(run_id: str, agent_id: str) -> None:
    """Booking must never turn a finished run into a failure; unbooked costs are picked up by the next call."""
    try:
        book_ai_costs()
    except Exception as exc:
        record_error("agent", exc, run_id=run_id, context={"agent_id": agent_id, "step": "book_ai_costs"})


def _step_decision(content: str) -> str:
    if content.startswith("ESCALATED"):
        return "escalated"
    if content.startswith("DENIED"):
        return "denied"
    if content.startswith("ERROR"):
        return "error"
    return "ok"


CONSULT_MAX_PER_TURN = 2


def _build_consult_tool(agent_id: str, run_id: str, depth: int) -> StructuredTool:
    """Chat-only tool: ask another eligible agent a question in chat mode, one level deep, rate-limited per turn.

    Built here (not in the catalog registry) because it only makes sense inside a chat run: it recurses into
    ``_run_core`` directly and must never be offered to an agent it consulted (``depth`` stops it from going
    further; ``consulted`` caps how many calls one turn may make). It still runs through the same policy gate as
    every other tool (``policy.yaml``'s ``consult_agent`` rule, T1, restricted to the chat-eligible agents) so the
    kill switch, safe mode, agents-paused and audit trail all apply; it is never added to a persona's TOOLS.md, so
    automated (non-chat) runs never see it.
    """
    consulted = [0]

    async def consult_agent(agent: str, question: str) -> str:
        """Ask another eligible agent a question and get their answer (one level deep; at most 2 per turn)."""
        eligible = get_settings().chat_agents
        if agent not in eligible or agent == agent_id:
            return f"ERROR: '{agent}' is not available to consult (eligible: {', '.join(eligible)})."
        if consulted[0] >= CONSULT_MAX_PER_TURN:
            return f"ERROR: consult_agent limit ({CONSULT_MAX_PER_TURN} per turn) reached."

        async def execute(args: dict[str, Any]) -> str:
            consulted[0] += 1
            sub = await _run_core(args["agent"], args["question"], run_id=f"{run_id}-consult{consulted[0]}",
                                  chat=True, depth=depth + 1)
            return json.dumps({"output": sub.output, "status": sub.status, "messages": sub.messages},
                              default=str)[:12000]

        return await run_gated(agent_id, "consult_agent", {"agent": agent, "question": question}, execute,
                               side_effect=True)

    return StructuredTool.from_function(coroutine=consult_agent, name="consult_agent",
                                        description=inspect.getdoc(consult_agent) or "consult_agent",
                                        metadata={"terminal": False, "side_effect": True})


async def run_agent(agent_id: str, task: str, *, context: str = "", run_id: str | None = None) -> AgentResult:
    """Automated entry point (handlers, scheduler, scenarios, chaos): a thin wrapper over ``_run_core`` so their
    behaviour stays byte-identical. Chat (dashboard, API, messaging channels) calls ``_run_core`` directly."""
    return await _run_core(agent_id, task, context=context, run_id=run_id)


async def _run_core(agent_id: str, task: str, *, context: str = "", run_id: str | None = None,
                    history: list[BaseMessage] | None = None, chat: bool = False,
                    on_event: Callable[[dict[str, Any]], None] | None = None, depth: int = 0) -> AgentResult:
    """Shared run loop. With ``chat=True``: terminal tools are dropped, a ``consult_agent`` tool is offered at
    ``depth`` 0 only, the whole history is checked for taint, and ``on_event`` (if given) streams the model's reply
    token by token plus tool_start/tool_end/escalation events."""
    settings = get_settings()
    spec = load_spec(agent_id)
    run_id = run_id or current_run.get() or new_id("run")
    t_run, t_agent = current_run.set(run_id), current_agent.set(agent_id)
    history = history or []
    # I15: a run starts clean unless its task, context or history already carries fenced (untrusted) content.
    history_texts = [m.content for m in history if isinstance(m.content, str)]
    reset_taint(task, context, *history_texts)
    row_id: str | None = None
    result = AgentResult(agent_id, "", status="max_steps")
    tin = tout = steps = 0  # kept outside the try: a crash after the model ran still has to pay for its tokens
    try:
        if not agent_enabled(agent_id):  # no LLM call and no AgentRun row; reflexes don't come through here
            audit("system", "agent_run", "deny", {"reason": "agent disabled", "agent_id": agent_id, "task": task[:200]}, run_id)
            return AgentResult(agent_id, f"{agent_id} is disabled (setting agent_enabled:{agent_id}=off); task not run.",
                               status="disabled")
        if spent_today(agent_id) >= daily_budget(agent_id):
            audit(agent_id, "agent_run", "deny", {"reason": "daily LLM budget exhausted", "task": task[:200]}, run_id)
            return AgentResult(agent_id, f"{agent_id} daily budget exhausted; task deferred.", status="budget_exceeded")

        with session() as s:
            row = AgentRun(run_id=run_id, agent_id=agent_id, model=spec.model if settings.llm_mode != "scripted" else "scripted",
                           task=task[:4000])
            s.add(row)
            s.flush()
            row_id = row.id
            lessons = list(s.scalars(select(Lesson.lesson).where(Lesson.agent_id == agent_id, Lesson.status == "applied")))

        prompt = system_prompt(spec, lessons) + "\n".join(["", *skills_for(agent_id)])
        tools = await build_tools(agent_id, context=task)
        if chat:
            tools = [t for t in tools if not t.metadata.get("terminal")]  # a chat turn never ends in a terminal tool
            if depth == 0:
                tools = [*tools, _build_consult_tool(agent_id, run_id, depth)]
        by_name = {t.name: t for t in tools}
        model = get_model(spec.model_tier, agent_id)

        def bind(server_tools: list[str], searches_left: int) -> Any:
            bindable: list[Any] = [*tools, *server_tool_specs(server_tools, searches_left)]
            return model.bind_tools(bindable) if bindable else model

        def searches_left() -> int:
            return max(0, settings.web_search_max_per_run - result.web_searches)

        server_tools = list(spec.server_tools)
        bound_left = searches_left()
        bound = bind(server_tools, bound_left)

        system: SystemMessage = SystemMessage(
            content=prompt if settings.llm_mode == "scripted"
            else [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}])  # cache the long persona prompt
        messages: list[BaseMessage] = [system, *history,
                                       HumanMessage(content=task + (f"\n\n# Context\n{context}" if context else ""))]
        step_log: list[dict[str, Any]] = []

        for steps in range(1, settings.agent_max_steps + 1):
            # max_uses only limits one API call; clip it to what is left of the run cap (rebinding when that shrinks
            # below the per-call max_uses) and drop web_search entirely once the cap is reached.
            if "web_search" in server_tools:
                left = searches_left()
                if left <= 0:
                    server_tools.remove("web_search")
                    bound = bind(server_tools, 0)
                    audit(agent_id, "web_search_cap", "ok", {"web_searches": result.web_searches,
                                                             "max_per_run": settings.web_search_max_per_run}, run_id)
                elif min(left, settings.web_search_max_uses) != min(bound_left, settings.web_search_max_uses):
                    bound_left = left
                    bound = bind(server_tools, left)
            if on_event is not None:  # streaming: model.astream (scripted mode's default astream yields one chunk)
                ai = None
                async for chunk in bound.astream(messages):
                    piece = _text(chunk) if not isinstance(chunk.content, str) else chunk.content
                    if piece:
                        on_event({"type": "text_delta", "agent_id": agent_id, "text": piece})
                    ai = chunk if ai is None else ai + chunk
                ai = ai if ai is not None else AIMessage(content="")
            else:
                ai = await bound.ainvoke(messages)
            usage = ai.usage_metadata or {}
            tin += usage.get("input_tokens", 0)
            tout += usage.get("output_tokens", 0)
            searches, fetches = server_tool_usage(ai)
            result.web_searches += searches
            result.web_fetches += fetches
            messages.append(ai)
            if not ai.tool_calls:
                result.output, result.status = _text(ai), "ok"
                step_log.append({"role": "assistant", "content": result.output, "tool_steps": []})
                break
            done = False
            tool_steps: list[dict[str, Any]] = []
            for call in ai.tool_calls:
                tool = by_name.get(call["name"])
                if on_event is not None:
                    on_event({"type": "tool_start", "agent_id": agent_id, "name": call["name"], "args": call["args"]})
                content = (await tool.ainvoke(call["args"])) if tool else f"ERROR: tool {call['name']} is not available to you."
                result.tool_calls += 1
                decision = _step_decision(content)
                step: dict[str, Any] = {"name": call["name"], "args": call["args"], "decision": decision,
                                        "result": content[:500],
                                        "terminal": bool(tool is not None and tool.metadata.get("terminal"))}
                if decision == "escalated":
                    approval_id = content.split("id=")[1].split(")")[0]
                    result.escalations.append(approval_id)
                    step["approval_id"] = approval_id
                tool_steps.append(step)
                if on_event is not None:
                    on_event({"type": "tool_end", "agent_id": agent_id, "name": call["name"], "decision": decision})
                    if decision == "escalated":
                        on_event({"type": "escalation", "agent_id": agent_id, "name": call["name"],
                                 "approval_id": step["approval_id"]})
                messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
                if tool is not None and tool.metadata.get("terminal") and not content.startswith(("DENIED", "ERROR")):
                    result.structured = json.loads(content)
                    result.output = json.dumps(result.structured, indent=1)
                    done = True
            step_log.append({"role": "assistant", "content": _text(ai), "tool_steps": tool_steps})
            if done:
                result.status = "ok"
                break
        else:
            result.output = _text(messages[-1]) if isinstance(messages[-1], AIMessage) else "stopped: max steps reached"

        result.messages = step_log
        result.cost_eur = (settings.cost_eur(spec.model_tier, tin, tout)
                           + settings.web_cost_eur(result.web_searches, result.web_fetches))
        with session() as s:
            row = s.get(AgentRun, row_id)
            row.output, row.status, row.steps, row.tool_calls = result.output[:8000], result.status, steps, result.tool_calls
            row.web_searches, row.web_fetches = result.web_searches, result.web_fetches
            row.input_tokens, row.output_tokens, row.cost_eur, row.ended_at = tin, tout, result.cost_eur, now()
        _book_ai_costs_safe(run_id, agent_id)
        return result
    except Exception as exc:
        error = mask(f"{type(exc).__name__}: {exc}")
        record_error("agent", exc, run_id=run_id, context={"agent_id": agent_id, "task": task[:200]})
        audit(agent_id, "agent_run", "error", {"error": error, "task": task[:200]}, run_id)
        if row_id is not None:  # close the run row, otherwise it shows as 'running' forever
            with session() as s:
                row = s.get(AgentRun, row_id)
                row.status, row.output, row.ended_at = "error", f"ERROR: {error}"[:8000], now()
                if tin or tout or result.web_searches or result.web_fetches:
                    try:
                        row.cost_eur = (settings.cost_eur(spec.model_tier, tin, tout)
                                        + settings.web_cost_eur(result.web_searches, result.web_fetches))
                        row.input_tokens, row.output_tokens = tin, tout
                        row.web_searches, row.web_fetches = result.web_searches, result.web_fetches
                    except Exception as cost_exc:
                        record_error("agent", cost_exc, run_id=run_id, context={"agent_id": agent_id, "step": "crash_cost"})
            _book_ai_costs_safe(run_id, agent_id)
        return AgentResult(agent_id, f"ERROR: {error}", status="error")
    finally:
        current_agent.reset(t_agent)
        current_run.reset(t_run)
