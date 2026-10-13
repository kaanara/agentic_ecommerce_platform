"""Company graph:  CEO directive → Foreman plan → departments (parallel waves) → Judge → (rework | CEO close).

    START → ceo_direct → foreman_plan → execute → judge ─┬─(rejected, retries left)→ foreman_plan
                                                          └─(approved / out of retries)→ ceo_close → END

State is checkpointed per run (thread_id = run_id) so runs can be inspected or replayed.
"""

from __future__ import annotations

import asyncio
import json
import operator
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, TypedDict

from langgraph.graph import END, START, StateGraph

from aecom.audit import audit
from aecom.config import get_settings
from aecom.db import new_id
from aecom.graph.agents import run_agent

DEPARTMENTS = {"store_ops", "marketing", "support", "finance", "supply", "researcher"}
MAX_REWORK = 1


class CompanyState(TypedDict, total=False):
    run_id: str
    directive: str
    ceo_brief: str
    plan: list[dict[str, Any]]
    plan_rationale: str
    results: Annotated[list[dict[str, Any]], operator.add]
    verdict: dict[str, Any]
    iterations: int
    summary: str
    escalations: Annotated[list[str], operator.add]


async def ceo_direct(state: CompanyState) -> CompanyState:
    r = await run_agent("ceo", (
        f"New directive or trigger:\n{state['directive']}\n\n"
        "Ground yourself with your tools (OKRs, decisions, P&L/KPIs) as needed, then write a brief for the Foreman: "
        "goal, constraints (budget, brand, compliance), success criteria tied to KPIs, and anything that needs your "
        "or a human's approval. Keep it under 250 words."), run_id=state["run_id"])
    return {"ceo_brief": r.output, "escalations": r.escalations}


async def foreman_plan(state: CompanyState) -> CompanyState:
    feedback = ""
    if state.get("verdict") and not state["verdict"].get("approved"):
        prior = "\n".join(f"- [{x['agent']}] {x['output'][:600]}" for x in state.get("results", []))
        feedback = (f"\n\n# The Judge rejected the previous attempt\n{state['verdict'].get('feedback')}\n"
                    f"Issues: {state['verdict'].get('issues')}\n# Previous results\n{prior}\nPlan only the rework.")
    r = await run_agent("foreman", (
        f"# CEO brief\n{state['ceo_brief']}{feedback}\n\n"
        f"Decompose into tasks for: {', '.join(sorted(DEPARTMENTS))}. Use depends_on for ordering; independent tasks run "
        "in parallel. Each instruction must be self-contained (SKUs, amounts, channels). Finish with submit_plan."),
        run_id=state["run_id"])
    tasks = (r.structured or {}).get("tasks", [])
    valid = [t for t in tasks if t.get("agent") in DEPARTMENTS]
    if len(valid) != len(tasks):
        audit("foreman", "plan", "info", {"dropped": [t for t in tasks if t not in valid]}, state["run_id"])
    return {"plan": valid, "plan_rationale": (r.structured or {}).get("rationale", r.output),
            "iterations": state.get("iterations", 0) + (1 if feedback else 0), "escalations": r.escalations}


def _waves(tasks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    done: set[str] = set()
    pending = list(tasks)
    waves = []
    while pending:
        ready = [t for t in pending if set(t.get("depends_on") or []) <= done]
        if not ready:  # cycle or unknown dependency → run the rest sequentially
            ready = [pending[0]]
        waves.append(ready)
        done |= {t["id"] for t in ready}
        pending = [t for t in pending if t not in ready]
    return waves


async def execute(state: CompanyState) -> CompanyState:
    outputs: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    escalations: list[str] = []
    for wave in _waves(state.get("plan", [])):
        async def run(task: dict[str, Any]) -> dict[str, Any]:
            deps = "\n\n".join(f"## Output of {d}\n{outputs.get(d, '')[:3000]}" for d in task.get("depends_on") or [])
            r = await run_agent(task["agent"], task["instruction"],
                                context=f"## CEO brief\n{state['ceo_brief']}\n\n{deps}".strip(), run_id=state["run_id"])
            return {"task_id": task["id"], "agent": task["agent"], "instruction": task["instruction"], "output": r.output,
                    "status": r.status, "escalations": r.escalations}

        for res in await asyncio.gather(*(run(t) for t in wave)):
            outputs[res["task_id"]] = res["output"]
            escalations += res["escalations"]
            results.append(res)
    return {"results": results, "escalations": escalations}


async def judge(state: CompanyState) -> CompanyState:
    latest = state.get("results", [])[-len(state.get("plan", [])) or None:]
    work = "\n\n".join(f"### {x['task_id']} — {x['agent']} ({x['status']})\nTask: {x['instruction']}\nResult:\n{x['output'][:2500]}"
                       for x in latest)
    r = await run_agent("judge", (
        f"# CEO brief\n{state['ceo_brief']}\n\n# Delivered work\n{work or '(nothing delivered)'}\n\n"
        "Score against your rubric. Pending human approvals are acceptable and not a failure by themselves. "
        "Finish with submit_verdict."), run_id=state["run_id"])
    verdict = r.structured or {"approved": False, "score": 0, "feedback": r.output, "issues": ["judge gave no verdict"]}
    return {"verdict": verdict, "escalations": r.escalations}


def after_judge(state: CompanyState) -> str:
    if state["verdict"].get("approved") or state.get("iterations", 0) >= MAX_REWORK:
        return "ceo_close"
    return "foreman_plan"


async def ceo_close(state: CompanyState) -> CompanyState:
    pending = sorted(set(state.get("escalations", [])))
    r = await run_agent("ceo", (
        f"# Your brief\n{state['ceo_brief']}\n\n# Judge verdict\n{json.dumps(state['verdict'])}\n\n"
        f"# Results\n" + "\n".join(f"- [{x['agent']}] {x['output'][:500]}" for x in state.get("results", [])) +
        f"\n\n# Pending human approvals\n{pending or 'none'}\n\n"
        "Close the run: a short executive summary, OKR impact, and next steps. Record a decision if you made one."),
        run_id=state["run_id"])
    return {"summary": r.output, "escalations": r.escalations}


def build_company_graph() -> StateGraph:
    g = StateGraph(CompanyState)
    g.add_node("ceo_direct", ceo_direct)
    g.add_node("foreman_plan", foreman_plan)
    g.add_node("execute", execute)
    g.add_node("judge", judge)
    g.add_node("ceo_close", ceo_close)
    g.add_edge(START, "ceo_direct")
    g.add_edge("ceo_direct", "foreman_plan")
    g.add_edge("foreman_plan", "execute")
    g.add_edge("execute", "judge")
    g.add_conditional_edges("judge", after_judge, ["foreman_plan", "ceo_close"])
    g.add_edge("ceo_close", END)
    return g


@asynccontextmanager
async def checkpointer() -> AsyncIterator[Any]:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from pathlib import Path

        db = Path(url.removeprefix("sqlite:///"))
        path = str(db.with_name(db.stem + ".checkpoints.db"))
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            yield saver
    else:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(url.replace("postgresql+psycopg", "postgresql")) as saver:
            await saver.setup()
            yield saver


async def run_company(directive: str, run_id: str | None = None) -> CompanyState:
    run_id = run_id or new_id("run")
    audit("system", "company_run", "info", {"directive": directive[:500]}, run_id)
    async with checkpointer() as saver:
        graph = build_company_graph().compile(checkpointer=saver)
        return await graph.ainvoke({"run_id": run_id, "directive": directive, "iterations": 0},
                                   config={"configurable": {"thread_id": run_id}, "recursion_limit": 40})
