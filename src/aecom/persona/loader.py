"""Load an agent from agents/<id>/{IDENTITY,SOUL,AGENTS,TOOLS,POLICY}.md.

IDENTITY.md, TOOLS.md and POLICY.md carry YAML frontmatter (machine-readable); the markdown bodies are
for humans. SOUL.md and AGENTS.md bodies become the system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from aecom.config import get_settings

REQUIRED_FILES = ("IDENTITY.md", "SOUL.md", "AGENTS.md", "TOOLS.md", "POLICY.md")


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        _, fm, body = text.split("---", 2)
        return yaml.safe_load(fm) or {}, body.strip()
    return {}, text.strip()


@dataclass
class AgentSpec:
    id: str
    display_name: str
    role: str
    model_tier: str
    reports_to: str | None
    emoji: str
    version: str
    soul: str
    procedures: str
    tools: list[str]
    server_tools: list[str]
    budget_eur_per_day: float | None
    identity: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return get_settings().model_for_tier(self.model_tier)


def agent_ids() -> list[str]:
    root = get_settings().agents_dir
    return sorted(p.name for p in root.iterdir() if (p / "IDENTITY.md").exists())


def _effective_text(agent_id: str, file_name: str, overrides: dict[str, str] | None = None) -> str:
    """``agents/<agent_id>/<file_name>``, with a UI override (``aecom.persona.overrides``) preferred over the file
    in git. ``overrides`` lets a not-yet-saved edit be validated before it is persisted (the rest of the files still
    use whatever is currently effective: an existing override, or the file)."""
    if overrides is not None and file_name in overrides:
        return overrides[file_name]
    from aecom.persona import overrides as persona_overrides

    found = persona_overrides.get_override(agent_id, file_name)
    if found is not None:
        return found
    return (get_settings().agents_dir / agent_id / file_name).read_text()


def _build_spec(agent_id: str, overrides: dict[str, str] | None = None) -> AgentSpec:
    folder: Path = get_settings().agents_dir / agent_id
    missing = [f for f in REQUIRED_FILES if not (folder / f).exists()]
    if missing:
        raise FileNotFoundError(f"agent {agent_id} is missing {missing}")
    identity, _ = split_frontmatter(_effective_text(agent_id, "IDENTITY.md", overrides))
    _, soul = split_frontmatter(_effective_text(agent_id, "SOUL.md", overrides))
    _, procedures = split_frontmatter(_effective_text(agent_id, "AGENTS.md", overrides))
    tools_fm, _ = split_frontmatter(_effective_text(agent_id, "TOOLS.md", overrides))
    policy_fm, _ = split_frontmatter(_effective_text(agent_id, "POLICY.md", overrides))
    if identity.get("id") != agent_id:
        raise ValueError(f"IDENTITY.md id '{identity.get('id')}' does not match folder '{agent_id}'")
    if identity.get("model_tier") not in ("opus", "sonnet", "haiku"):
        raise ValueError(f"{agent_id}: model_tier must be opus | sonnet | haiku")
    return AgentSpec(
        id=agent_id,
        display_name=identity["display_name"],
        role=identity["role"],
        model_tier=identity["model_tier"],
        reports_to=identity.get("reports_to"),
        emoji=identity.get("emoji", ""),
        version=str(identity.get("version", "1")),
        soul=soul,
        procedures=procedures,
        tools=list(tools_fm.get("tools", [])),
        server_tools=list(tools_fm.get("server_tools", [])),
        budget_eur_per_day=policy_fm.get("budget_eur_per_day"),
        identity=identity,
    )


@lru_cache
def load_spec(agent_id: str) -> AgentSpec:
    return _build_spec(agent_id)


def preview_spec(agent_id: str, overrides: dict[str, str]) -> AgentSpec:
    """Build a spec from proposed override content, without persisting or caching it - used by
    ``aecom.persona.overrides.save_override`` to validate an edit before it is saved."""
    return _build_spec(agent_id, overrides)


def company_doc_text(file_name: str) -> str:
    """A ``company/`` document (e.g. ``GOALS.md``), with a UI override (scope ``"company"``) preferred over the
    file in git. Empty string if neither exists."""
    from aecom.persona import overrides as persona_overrides

    override = persona_overrides.get_override("company", file_name)
    if override is not None:
        return override
    path = get_settings().company_dir / file_name
    return path.read_text() if path.exists() else ""
