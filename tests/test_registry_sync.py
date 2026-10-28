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


def _revoke_audits() -> list[AuditLog]:
    with session() as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action == "allowlist_revoke")))


def test_revoke_removes_audits_and_second_revoke_is_a_noop():
    assert "reply_email" in allowed_tools("support")
    res = registry.revoke("support", "reply_email", "kaan", note="too risky")
    assert res["removed"] is True
    assert "reply_email" not in allowed_tools("support")
    (a,) = _revoke_audits()
    assert a.actor == "human:kaan" and a.decision == "revoked"
    assert a.detail["agent"] == "support" and a.detail["tool"] == "reply_email" and a.detail["note"] == "too risky"
    again = registry.revoke("support", "reply_email", "kaan")
    assert again["removed"] is False and "warning" not in again
    assert len(_revoke_audits()) == 1


def test_revoke_warns_when_tools_md_still_lists_it_and_init_readds_it():
    res = registry.revoke("support", "reply_email", "kaan")
    assert "TOOLS.md still lists reply_email" in res["warning"]
    summary = seed_registry()  # documented: the additive sync restores tools TOOLS.md lists
    assert ("support", "reply_email") in summary["added"]
    assert "reply_email" in allowed_tools("support")


def test_revoke_no_warning_for_tool_not_in_tools_md_and_stays_revoked():
    from aecom.persona.loader import load_spec

    with session() as s:
        s.add(Allowlist(agent_id="support", tool_name="get_kpis"))
    assert "get_kpis" not in load_spec("support").tools
    res = registry.revoke("support", "get_kpis", "kaan")
    assert res["removed"] and "warning" not in res
    seed_registry()
    assert "get_kpis" not in allowed_tools("support")


def test_list_allowlists_filters_by_agent():
    everything = registry.list_allowlists()
    assert "support" in everything and len(everything) > 1
    assert list(registry.list_allowlists("support")) == ["support"]
    assert registry.list_allowlists("nobody") == {}


def test_only_leftover_builtin_tools_are_warned_about_not_granted_skills(caplog):
    """Runbook/policy skills are granted via the registry on purpose; a leftover built-in tool is the real signal."""
    import logging

    from aecom.db import Allowlist, session
    from aecom.registry.tools import seed_registry

    with session() as s:
        s.add(Allowlist(agent_id="sentinel", tool_name="request_account_need"))  # built-in, not in its TOOLS.md
    with caplog.at_level(logging.WARNING, logger="aecom.registry.tools"):
        out = seed_registry()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "sentinel/request_account_need" in text and "registry revoke" in text
    assert "runbook_" not in text and "scraping_policy" not in text
    assert any(t.startswith("runbook_") for _, t in out["extra"])  # still reported in the return value
