"""seed_registry() syncs TOOLS.md additions into existing allowlists, never removes runtime grants."""

from dataclasses import replace

import pytest
from sqlalchemy import delete, func, select

from aecom.db import Allowlist, AuditLog, session
from aecom.registry import tools as registry
from aecom.registry.tools import allowed_tools, seed_registry


def _sync_audits() -> int:
    with session() as s:
        return s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "allowlist_sync"))


def _simulate_db_predating(agent_id: str, tool_name: str) -> None:
    """An existing DB seeded before `tool_name` was added to the agent's TOOLS.md."""
    with session() as s:
        s.execute(delete(Allowlist).where(Allowlist.agent_id == agent_id, Allowlist.tool_name == tool_name))
    assert tool_name not in allowed_tools(agent_id)
    assert allowed_tools(agent_id)  # the agent still has other rows, i.e. is "already seeded"


def test_newly_listed_tool_reaches_existing_allowlist():
    _simulate_db_predating("support", "reply_email")
    before = _sync_audits()
    summary = seed_registry()
    assert ("support", "reply_email") in summary["added"]
    assert "reply_email" in allowed_tools("support")
    assert _sync_audits() == before + len(summary["added"])
    with session() as s:
        details = [a.detail for a in s.scalars(select(AuditLog).where(AuditLog.action == "allowlist_sync"))]
    assert {"agent_id": "support", "tool": "reply_email"} in details


def test_runtime_grant_kept_and_reported_as_extra():
    from aecom.tools.catalog import grant_tool

    assert "publish_social_post" not in registry.load_spec("support").tools
    grant_tool("publish_social_post", "support")
    summary = seed_registry()
    assert ("support", "publish_social_post") in summary["extra"]
    assert "publish_social_post" in allowed_tools("support")


def test_resync_is_idempotent():
    _simulate_db_predating("support", "reply_email")
    seed_registry()
    audits = _sync_audits()
    summary = seed_registry()
    assert summary["added"] == []
    assert _sync_audits() == audits
    with session() as s:
        rows = s.execute(select(Allowlist.agent_id, Allowlist.tool_name)).all()
    assert len(rows) == len(set(rows))


def test_unknown_tool_still_raises(monkeypatch):
    real = registry.load_spec

    def fake(agent_id):
        spec = real(agent_id)
        return replace(spec, tools=[*spec.tools, "no_such_tool"]) if agent_id == "support" else spec

    monkeypatch.setattr(registry, "load_spec", fake)
    with pytest.raises(ValueError, match="no_such_tool"):
        seed_registry()
