"""UI-edited persona and company files (STUB: built in W2 builder S2). The loader prefers an override over the file in git.

``scope`` is an agent id or ``company``; ``file_name`` e.g. ``SOUL.md``. TOOLS.md changes still go through the C5
allowlist resync (additive, human-confirmed).
"""

from __future__ import annotations

from typing import Any


def get_override(scope: str, file_name: str) -> str | None:
    raise NotImplementedError


def save_override(scope: str, file_name: str, content: str, by: str) -> None:
    raise NotImplementedError


def reset_override(scope: str, file_name: str, by: str) -> None:
    raise NotImplementedError


def list_overrides() -> list[dict[str, Any]]:
    raise NotImplementedError
