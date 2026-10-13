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


@lru_cache
def load_spec(agent_id: str) -> AgentSpec:
    folder: Path = get_settings().agents_dir / agent_id
    missing = [f for f in REQUIRED_FILES if not (folder / f).exists()]
    if missing:
        raise FileNotFoundError(f"agent {agent_id} is missing {missing}")
    identity, _ = split_frontmatter((folder / "IDENTITY.md").read_text())
    _, soul = split_frontmatter((folder / "SOUL.md").read_text())
    _, procedures = split_frontmatter((folder / "AGENTS.md").read_text())
    tools_fm, _ = split_frontmatter((folder / "TOOLS.md").read_text())
    policy_fm, _ = split_frontmatter((folder / "POLICY.md").read_text())
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
