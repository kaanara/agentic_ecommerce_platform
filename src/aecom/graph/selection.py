"""Generic selection workflow — e-commerce platform, payment/MoR, ad platforms, social, email tools, MCP servers.

    researcher (brief) → finance (cost/ROI) → judge (scorecard) → ceo (decide → ADR → provisioning request)
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from aecom.db import new_id
from aecom.graph.agents import run_agent
from aecom.graph.supervisor import checkpointer

CATEGORIES = {
    "ecommerce_platform": "storefront/checkout platform for selling digital products (licence keys, downloads, courses)",
    "payment": "payment provider or merchant-of-record handling EU VAT/OSS for digital goods",
    "ad_platform": "paid advertising platforms for acquiring buyers of digital products",
    "social": "organic social channels for brand building and community",
    "email_marketing": "newsletter / lifecycle email tooling",
    "mcp": "MCP servers that give agents new capabilities",
}


class SelectionState(TypedDict, total=False):
    run_id: str
    category: str
    need: str
    candidates: list[str]
    brief: dict[str, Any]
    finance: str
    scorecard: dict[str, Any]
    decision: str


async def research(state: SelectionState) -> SelectionState:
    r = await run_agent("researcher", (
        f"Selection: {state['category']} — {CATEGORIES.get(state['category'], '')}.\nNeed: {state['need']}\n"
        f"Candidates to consider (add others if clearly better): {', '.join(state.get('candidates') or []) or 'your choice'}\n"
        "For each candidate: fit for digital products, fees, EU VAT handling, API/MCP availability for automation, "
        "lock-in, risks. Cite sources. Finish with submit_brief."), run_id=state["run_id"])
    return {"brief": r.structured or {"summary": r.output}}


async def finance(state: SelectionState) -> SelectionState:
    r = await run_agent("finance", (
        f"Cost/ROI assessment for {state['category']} candidates.\nResearch brief:\n{json.dumps(state['brief'], indent=1)}\n"
        "Model monthly fixed cost, % fees at €2k, €10k and €50k monthly revenue, and payback. Recommend within budget."),
        run_id=state["run_id"])
    return {"finance": r.output}


async def judge(state: SelectionState) -> SelectionState:
    r = await run_agent("judge", (
        f"Score the {state['category']} candidates 0-10 on: strategic fit, total cost, compliance (GDPR/VAT), "
        "automation/API quality, risk/lock-in. Put per-candidate scores in `issues` as 'name: s1/s2/s3/s4/s5 = total'. "
        "approved=true if at least one candidate is acceptable.\n\n"
        f"Brief:\n{json.dumps(state['brief'], indent=1)}\n\nFinance:\n{state['finance']}\n\nFinish with submit_verdict."),
        run_id=state["run_id"])
    return {"scorecard": r.structured or {"feedback": r.output}}


async def ceo_decide(state: SelectionState) -> SelectionState:
    r = await run_agent("ceo", (
        f"Decide the {state['category']}.\nNeed: {state['need']}\nBrief:\n{json.dumps(state['brief'], indent=1)}\n"
        f"Finance:\n{state['finance']}\nJudge scorecard:\n{json.dumps(state['scorecard'], indent=1)}\n\n"
        "1) record_decision with rationale, alternatives and scorecard. 2) If the choice needs an account or "
        "credentials, call request_provisioning with concrete human steps and the env var name for the credential."),
        run_id=state["run_id"])
    return {"decision": r.output}


def build_selection_graph() -> StateGraph:
    g = StateGraph(SelectionState)
    for name, fn in [("research", research), ("finance", finance), ("judge", judge), ("ceo_decide", ceo_decide)]:
        g.add_node(name, fn)
    g.add_edge(START, "research")
    g.add_edge("research", "finance")
    g.add_edge("finance", "judge")
    g.add_edge("judge", "ceo_decide")
    g.add_edge("ceo_decide", END)
    return g


async def run_selection(category: str, need: str, candidates: list[str] | None = None, run_id: str | None = None) -> SelectionState:
    run_id = run_id or new_id("sel")
    async with checkpointer() as saver:
        graph = build_selection_graph().compile(checkpointer=saver)
        return await graph.ainvoke({"run_id": run_id, "category": category, "need": need, "candidates": candidates or []},
                                   config={"configurable": {"thread_id": run_id}})
