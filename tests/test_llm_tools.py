"""Anthropic server-side tool specs (web_search / web_fetch)."""

from aecom.config import get_settings
from aecom.llm import server_tool_specs
from aecom.persona.loader import load_spec


def test_server_tool_specs_anthropic_mode(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_mode", "anthropic")
    monkeypatch.setattr(s, "web_fetch_tool_type", "web_fetch_test")
    monkeypatch.setattr(s, "web_fetch_max_uses", 3)
    specs = server_tool_specs(["web_search", "web_fetch"])
    assert specs == [
        {"type": s.web_search_tool_type, "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_test", "name": "web_fetch", "max_uses": 3,
         "max_content_tokens": s.web_fetch_max_content_tokens},
    ]
    assert server_tool_specs(["web_fetch"]) == [specs[1]]


def test_server_tool_specs_scripted_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_mode", "scripted")
    assert server_tool_specs(["web_search", "web_fetch"]) == []


def test_web_fetch_defaults():
    s = get_settings()
    assert s.web_fetch_tool_type.startswith("web_fetch_")
    assert s.web_fetch_max_uses > 0


def test_researcher_has_server_web_fetch():
    spec = load_spec("researcher")
    assert "web_fetch" in spec.server_tools
    assert "fetch_url" in spec.tools  # hardened fallback stays
