"""M3: the researcher's scraping policy is documented and seeded as a skill."""

from sqlalchemy import select

from aecom.bootstrap import SCRAPING_POLICY_SKILL, init, seed_scraping_policy_skill
from aecom.config import ROOT
from aecom.db import Allowlist, RegistryTool, session
from aecom.registry.tools import skills_for


def test_researcher_policy_documents_rules():
    text = (ROOT / "agents" / "researcher" / "POLICY.md").read_text()
    assert "robots.txt" in text
    assert "personal data about private individuals" in text
    assert "source URL" in text


def test_skill_seeded_once_approved_and_granted():
    with session() as s:
        row = s.get(RegistryTool, SCRAPING_POLICY_SKILL)
        assert row.kind == "skill" and row.status == "approved"
        assert "robots.txt" in row.config["instructions"]
    assert any("robots.txt" in x and SCRAPING_POLICY_SKILL in x for x in skills_for("researcher"))
    assert not any(SCRAPING_POLICY_SKILL in x for x in skills_for("sentinel"))


def test_second_init_does_not_duplicate():
    init()
    assert seed_scraping_policy_skill() is False
    with session() as s:
        assert len(s.scalars(select(RegistryTool).where(RegistryTool.name == SCRAPING_POLICY_SKILL)).all()) == 1
        assert len(s.scalars(select(Allowlist).where(Allowlist.tool_name == SCRAPING_POLICY_SKILL)).all()) == 1
