"""UI-edited persona and company files (``ops_persona_overrides``). ``aecom.persona.loader`` prefers an override
over the file in git, so files tracked by git are never edited on the Pi - ``scripts/update.sh``'s ``git pull``
never conflicts.

``scope`` is an agent id (``agents/<scope>/<file_name>``) or ``"company"`` (``company/<file_name>``). A ``TOOLS.md``
edit still goes through the existing C5 allowlist resync (``aecom.registry.tools.seed_registry``): additive only, so
a human still grants or revokes individual tools - this just lets the resync see the edited file.
"""

from __future__ import annotations

import hashlib
from typing import Any

from aecom.audit import audit
from aecom.config import get_settings
from aecom.db import PersonaOverride, now, session

TOOLS_FILE = "TOOLS.md"


def _path(scope: str, file_name: str):
    settings = get_settings()
    base = settings.company_dir if scope == "company" else settings.agents_dir / scope
    return base / file_name


def file_text(scope: str, file_name: str) -> str | None:
    """The tracked (git) file's content, or None if it does not exist."""
    path = _path(scope, file_name)
    return path.read_text() if path.exists() else None


def file_hash(scope: str, file_name: str) -> str | None:
    text = file_text(scope, file_name)
    return None if text is None else hashlib.sha256(text.encode()).hexdigest()


def _row(s: Any, scope: str, file_name: str) -> PersonaOverride | None:
    return s.query(PersonaOverride).filter_by(scope=scope, file_name=file_name).one_or_none()


def get_override(scope: str, file_name: str) -> str | None:
    with session() as s:
        row = _row(s, scope, file_name)
        return row.content if row else None


def get_override_detail(scope: str, file_name: str) -> dict[str, Any] | None:
    """Full row for the editor: content, who/when, and whether the tracked file changed underneath since the
    override was made (``stale``) - the base_file_hash warning."""
    with session() as s:
        row = _row(s, scope, file_name)
        if row is None:
            return None
        current_hash = file_hash(scope, file_name)
        return {"scope": row.scope, "file_name": row.file_name, "content": row.content,
                "base_file_hash": row.base_file_hash, "updated_by": row.updated_by, "updated_at": row.updated_at,
                "stale": bool(row.base_file_hash) and row.base_file_hash != current_hash}


def current_text(scope: str, file_name: str) -> str | None:
    """What the loader actually serves: the override if one exists, else the tracked file."""
    override = get_override(scope, file_name)
    return override if override is not None else file_text(scope, file_name)


def _resync_tools_if_needed(scope: str, file_name: str) -> None:
    if file_name == TOOLS_FILE and scope != "company":
        from aecom.registry.tools import seed_registry

        seed_registry()


def save_override(scope: str, file_name: str, content: str, by: str) -> None:
    """Validate (through ``aecom.persona.loader`` for an agent scope - it must still build a valid persona) and
    store. Raises ``ValueError`` for an unknown scope/file or an edit that would break the persona."""
    if not by.strip():
        raise ValueError("a name is needed to record who changed this")
    if scope != "company":
        from aecom.persona.loader import REQUIRED_FILES, load_spec, preview_spec

        if file_name not in REQUIRED_FILES:
            raise ValueError(f"{file_name} is not a persona file ({', '.join(REQUIRED_FILES)})")
    if file_text(scope, file_name) is None:
        raise ValueError(f"{scope}/{file_name} does not exist in git - overrides edit an existing file")
    if scope != "company":
        try:
            preview_spec(scope, {file_name: content})
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"would break {scope}'s persona: {exc}") from None
    base_hash = file_hash(scope, file_name) or ""
    with session() as s:
        row = _row(s, scope, file_name)
        if row is None:
            s.add(PersonaOverride(scope=scope, file_name=file_name, content=content, base_file_hash=base_hash,
                                  updated_by=by, updated_at=now()))
        else:
            row.content, row.base_file_hash, row.updated_by, row.updated_at = content, base_hash, by, now()
    if scope != "company":
        load_spec.cache_clear()
    audit(f"human:{by}", "persona_override_save", "ok", {"scope": scope, "file_name": file_name})
    _resync_tools_if_needed(scope, file_name)


def reset_override(scope: str, file_name: str, by: str) -> None:
    """Delete the override so the tracked file applies again."""
    if not by.strip():
        raise ValueError("a name is needed to record who changed this")
    with session() as s:
        row = _row(s, scope, file_name)
        if row is None:
            return
        s.delete(row)
    if scope != "company":
        from aecom.persona.loader import load_spec

        load_spec.cache_clear()
    audit(f"human:{by}", "persona_override_reset", "ok", {"scope": scope, "file_name": file_name})
    _resync_tools_if_needed(scope, file_name)


def list_overrides() -> list[dict[str, Any]]:
    with session() as s:
        rows = list(s.query(PersonaOverride).order_by(PersonaOverride.scope, PersonaOverride.file_name))
        return [{"scope": r.scope, "file_name": r.file_name, "updated_by": r.updated_by, "updated_at": r.updated_at,
                 "stale": bool(r.base_file_hash) and r.base_file_hash != file_hash(r.scope, r.file_name)}
               for r in rows]
