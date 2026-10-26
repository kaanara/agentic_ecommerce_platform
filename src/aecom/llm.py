"""Model factory.

``LLM_MODE=anthropic`` → ChatAnthropic per tier (Opus / Sonnet / Haiku).
``LLM_MODE=scripted``  → ScriptedChatModel: replays per-agent steps registered by a scenario. Lets every
scenario, test and dashboard run deterministically with no API key and no spend.
"""

from __future__ import annotations

import itertools
from collections import defaultdict, deque
from typing import Any, Callable

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aecom.config import get_settings
from aecom.secrets import secrets

Step = dict[str, Any] | Callable[[list[BaseMessage]], dict[str, Any]]


class Script:
    """Per-agent queues of scripted model turns. A step is
    {"tool_calls": [{"name": ..., "args": {...}}], "text": "..."} or {"text": "final answer"},
    or a callable receiving the conversation and returning such a dict."""

    queues: dict[str, deque[Step]] = defaultdict(deque)

    @classmethod
    def load(cls, script: dict[str, list[Step]]) -> None:
        cls.queues.clear()
        for agent_id, steps in script.items():
            cls.queues[agent_id].extend(steps)

    @classmethod
    def next(cls, agent_id: str, messages: list[BaseMessage]) -> dict[str, Any]:
        q = cls.queues.get(agent_id)
        if not q:
            return {"text": f"[{agent_id}] Nothing further to do (scripted)."}
        step = q.popleft()
        return step(messages) if callable(step) else step


_ids = itertools.count(1)


class ScriptedChatModel(BaseChatModel):
    agent_id: str

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedChatModel":  # tools are chosen by the script
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        step = Script.next(self.agent_id, messages)
        calls = [{"name": c["name"], "args": c.get("args", {}), "id": f"call_{next(_ids)}", "type": "tool_call"}
                 for c in step.get("tool_calls", [])]
        text = step.get("text", "")
        prompt_chars = sum(len(str(m.content)) for m in messages)
        msg = AIMessage(content=text, tool_calls=calls, usage_metadata={
            "input_tokens": prompt_chars // 4, "output_tokens": 60 + len(text) // 4,
            "total_tokens": prompt_chars // 4 + 60 + len(text) // 4})
        return ChatResult(generations=[ChatGeneration(message=msg)])


def get_model(tier: str, agent_id: str) -> BaseChatModel:
    settings = get_settings()
    if settings.llm_mode == "scripted":
        return ScriptedChatModel(agent_id=agent_id)
    from langchain_anthropic import ChatAnthropic

    secrets.export_prefixed("LANGSMITH_")  # LangChain tracing reads os.environ
    key = secrets.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing — set it in .env or run with LLM_MODE=scripted")
    return ChatAnthropic(model=settings.model_for_tier(tier), api_key=key, max_tokens=4096, max_retries=3,
                         timeout=120)


def server_tool_specs(names: list[str]) -> list[dict[str, Any]]:
    settings = get_settings()
    if settings.llm_mode == "scripted":
        return []
    specs = []
    if "web_search" in names:
        specs.append({"type": settings.web_search_tool_type, "name": "web_search", "max_uses": 5})
    if "web_fetch" in names:
        specs.append({"type": settings.web_fetch_tool_type, "name": "web_fetch",
                      "max_uses": settings.web_fetch_max_uses})
    return specs
