"""Runs one agent on one task: persona prompt + gated tools + model loop.

Kept as an explicit loop (rather than a prebuilt ReAct agent) so the budget check, cost accounting,
terminal "submit_*" tools and per-step audit are visible and easy to change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import func, select

from aecom import ledger
from aecom.audit import audit
from aecom.config import get_settings
from aecom.context import current_agent, current_run
from aecom.db import AgentRun, Lesson, new_id, now, session
from aecom.llm import get_model, server_tool_specs
from aecom.persona.loader import load_spec
from aecom.persona.prompt import system_prompt
from aecom.policy.gate import load_policy
from aecom.registry.tools import build_tools, skills_for


@dataclass
class AgentResult:
    agent_id: str
    output: str
    status: str = "ok"  # ok | error | budget_exceeded | max_steps
    structured: dict[str, Any] | None = None
    tool_calls: int = 0
    cost_eur: float = 0.0
    escalations: list[str] = field(default_factory=list)


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


async def run_agent(agent_id: str, task: str, *, context: str = "", run_id: str | None = None) -> AgentResult:
    settings = get_settings()
    spec = load_spec(agent_id)
    run_id = run_id or current_run.get() or new_id("run")
    t_run, t_agent = current_run.set(run_id), current_agent.set(agent_id)
    row_id: str | None = None
    try:
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
        by_name = {t.name: t for t in tools}
        model = get_model(spec.model_tier, agent_id)
        bindable: list[Any] = [*tools, *server_tool_specs(spec.server_tools)]
        bound = model.bind_tools(bindable) if bindable else model

        system: SystemMessage = SystemMessage(
            content=prompt if settings.llm_mode == "scripted"
            else [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}])  # cache the long persona prompt
        messages: list[BaseMessage] = [system, HumanMessage(content=task + (f"\n\n# Context\n{context}" if context else ""))]

        result = AgentResult(agent_id, "", status="max_steps")
        tin = tout = steps = 0
        for steps in range(1, settings.agent_max_steps + 1):
            ai: AIMessage = await bound.ainvoke(messages)
            usage = ai.usage_metadata or {}
            tin += usage.get("input_tokens", 0)
            tout += usage.get("output_tokens", 0)
            messages.append(ai)
            if not ai.tool_calls:
                result.output, result.status = _text(ai), "ok"
                break
            done = False
            for call in ai.tool_calls:
                tool = by_name.get(call["name"])
                content = (await tool.ainvoke(call["args"])) if tool else f"ERROR: tool {call['name']} is not available to you."
                result.tool_calls += 1
                if content.startswith("ESCALATED"):
                    result.escalations.append(content.split("id=")[1].split(")")[0])
                messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
                if tool is not None and tool.metadata.get("terminal") and not content.startswith(("DENIED", "ERROR")):
                    result.structured = json.loads(content)
                    result.output = json.dumps(result.structured, indent=1)
                    done = True
            if done:
                result.status = "ok"
                break
        else:
            result.output = _text(messages[-1]) if isinstance(messages[-1], AIMessage) else "stopped: max steps reached"

        result.cost_eur = settings.cost_eur(spec.model_tier, tin, tout)
        with session() as s:
            row = s.get(AgentRun, row_id)
            row.output, row.status, row.steps, row.tool_calls = result.output[:8000], result.status, steps, result.tool_calls
            row.input_tokens, row.output_tokens, row.cost_eur, row.ended_at = tin, tout, result.cost_eur, now()
        if result.cost_eur > 0:
            ledger.record_expense("ai_expense", result.cost_eur, f"LLM {agent_id} ({spec.model_tier})", row_id)
        return result
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        audit(agent_id, "agent_run", "error", {"error": error, "task": task[:200]}, run_id)
        if row_id is not None:  # close the run row, otherwise it shows as 'running' forever
            with session() as s:
                row = s.get(AgentRun, row_id)
                row.status, row.output, row.ended_at = "error", f"ERROR: {error}"[:8000], now()
        return AgentResult(agent_id, f"ERROR: {error}", status="error")
    finally:
        current_agent.reset(t_agent)
        current_run.reset(t_run)
