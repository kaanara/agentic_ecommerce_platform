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


def _domain_filter(allowed: list[str], blocked: list[str]) -> dict[str, list[str]]:
    # The API accepts allowed_domains or blocked_domains, never both.
    if allowed:
        return {"allowed_domains": list(allowed)}
    if blocked:
        return {"blocked_domains": list(blocked)}
    return {}


def server_tool_specs(names: list[str], web_searches_left: int | None = None) -> list[dict[str, Any]]:
    """Anthropic server-tool specs for ``names``.

    ``web_searches_left`` is what remains of the per-run search budget: web_search's ``max_uses`` is clipped to it so
    one API call cannot overshoot the run cap, and the tool is dropped once nothing is left.
    """
    settings = get_settings()
    if settings.llm_mode == "scripted":
        return []
    specs = []
    search_uses = settings.web_search_max_uses if web_searches_left is None \
        else min(settings.web_search_max_uses, web_searches_left)
    if "web_search" in names and search_uses > 0:
        specs.append({"type": settings.web_search_tool_type, "name": "web_search",
                      "max_uses": search_uses,
                      **_domain_filter(settings.web_search_allowed_domains, settings.web_search_blocked_domains)})
    if "web_fetch" in names:
        specs.append({"type": settings.web_fetch_tool_type, "name": "web_fetch",
                      "max_uses": settings.web_fetch_max_uses,
                      "max_content_tokens": settings.web_fetch_max_content_tokens,
                      **_domain_filter(settings.web_fetch_allowed_domains, settings.web_fetch_blocked_domains)})
    return specs


def server_tool_usage(msg: AIMessage) -> tuple[int, int]:
    """(web_search_requests, web_fetch_requests) reported for one model call.

    langchain-anthropic copies the raw Messages API response (minus content) into ``response_metadata``, so the
    counts sit at ``response_metadata["usage"]["server_tool_use"]``; ``usage_metadata`` carries tokens only.
    """
    usage = (msg.response_metadata or {}).get("usage") or {}
    stu = usage.get("server_tool_use") if isinstance(usage, dict) else getattr(usage, "server_tool_use", None)
    if not stu:
        return 0, 0
    if not isinstance(stu, dict):
        stu = {"web_search_requests": getattr(stu, "web_search_requests", 0),
               "web_fetch_requests": getattr(stu, "web_fetch_requests", 0)}
    return int(stu.get("web_search_requests") or 0), int(stu.get("web_fetch_requests") or 0)
