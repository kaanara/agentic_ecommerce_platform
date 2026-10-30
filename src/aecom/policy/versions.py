"""UI-edited policy versions (STUB: built in W2 builder S2). The gate loads ``active_yaml()`` when present, else the file.

Rules: every saved version must pass the existing policy loader's validation; ``loosens(old, new)`` is true when a rule
gets a lower tier, higher limits or any T4 rule changes - saving such a version needs ``step_up_ok=True``.
"""

from __future__ import annotations

from typing import Any


def active_yaml() -> str | None:
    raise NotImplementedError


def save_version(yaml_text: str, note: str, by: str, *, step_up_ok: bool = False) -> int:
    raise NotImplementedError


def list_versions(limit: int = 50) -> list[dict[str, Any]]:
    raise NotImplementedError


def activate(version_id: int, by: str, *, step_up_ok: bool = False) -> None:
    raise NotImplementedError


def loosens(old_yaml: str, new_yaml: str) -> bool:
    raise NotImplementedError
