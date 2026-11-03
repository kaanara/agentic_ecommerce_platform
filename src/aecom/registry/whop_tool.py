"""The agents' ``whop_request`` tool: one StructuredTool whose every call is gated under ``api:whop/<operation>``.

Built per agent in ``registry.tools.build_tools`` (instead of ``gate_builtin``, which would gate everything under the
single name ``whop_request``), and only when ``STORE_ADAPTER=whop``. The operation list is in ``store.whop_ops``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from aecom.config import get_settings
from aecom.context import current_agent
from aecom.policy.gate import run_gated
from aecom.store import whop_ops

TOOL_NAME = "whop_request"


def enabled() -> bool:
    return get_settings().store_adapter == "whop"


def build_whop_tool(agent_id: str, context: str = "") -> BaseTool:
    description = ("Call one allowlisted Whop operation. `operation` is one of the names below; `args` is one flat "
                   "dict of path parameters (id), then query or body fields. Writes follow the approval policy "
                   "(some wait for a human). Refunds: use refund_order. Prices: use update_price.\n"
                   + whop_ops.describe())

    def execute_for(operation: str):
        def run(args: dict[str, Any]) -> str:
            token = current_agent.set(agent_id)
            try:
                return whop_ops.execute(operation, args)
            finally:
                current_agent.reset(token)
        return run

    async def whop_request(operation: str, args: dict[str, Any] | None = None) -> str:
        args = dict(args or {})
        try:
            op, _, _ = whop_ops.prepare(operation, args, get_settings().whop_account_id)  # validate before the gate
        except whop_ops.OpError as exc:
            return f"ERROR: {exc}"
        try:
            return await run_gated(agent_id, whop_ops.policy_name(operation), args, execute_for(operation),
                                   side_effect=op.side_effect, context=context)
        except whop_ops.OpError as exc:
            return f"ERROR: {exc}"

    return StructuredTool.from_function(coroutine=whop_request, name=TOOL_NAME, description=description,
                                        metadata={"terminal": False, "side_effect": True})
