"""F6: platform skills (Etsy listing rules, Shopify SEO, EU VAT/OSS) shipped as files in skills/platform/.

There is no generic "load skills from a directory" mechanism in this repo (skills normally reach the registry one
of two ways: an agent calls `propose_tool(kind="skill", ...)` — see the "self-tooling" scenario — or a human-
reviewed doc is parsed at `aecom init` time, always landing as `approved_by="system"` for operational skills that
are already reviewed via the doc's own PR, e.g. `seed_runbook_skills`/`seed_scraping_policy_skill` in bootstrap.py).

These platform skills are vendor/compliance content (Etsy policy, Shopify SEO, EU VAT), not this app's own
operational runbooks, so they are *not* auto-approved the "system seed" way: each file's frontmatter declares
``status: proposed`` and nothing here promotes it further. A human (normally via the Foreman, exactly like the
"self-tooling" scenario's ``skill-eu-vat-pricing``) must still call ``sandbox_tool`` → ``approve_tool`` →
``grant_tool`` before any agent sees the content. These tests prove the file content is valid input to that real,
unmodified registry lifecycle (`aecom.tools.catalog.propose_tool` et al.) and that nothing here can skip it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from aecom.config import ROOT
from aecom.db import RegistryTool, session
from aecom.persona.loader import agent_ids, split_frontmatter
from aecom.registry.tools import skills_for
from aecom.tools.catalog import approve_tool, grant_tool, propose_tool, sandbox_tool

SKILLS_DIR = ROOT / "skills" / "platform"
SKILL_FILES = sorted(SKILLS_DIR.glob("*.md"))

ALLOWED_DOMAINS = {"etsy.com", "help.shopify.com", "ec.europa.eu", "vat-one-stop-shop.ec.europa.eu", "bzst.de"}
URL_RE = re.compile(r"https?://\S+")
BARE_DOMAIN_RE = re.compile(r"\b([a-z0-9.-]+\.[a-z]{2,})\b")
SECRET_PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)secret\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)password\s*[:=]\s*['\"]?\S{4,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def _load(path: Path) -> tuple[dict, str]:
    return split_frontmatter(path.read_text())


def test_three_platform_skill_files_exist():
    names = {p.name for p in SKILL_FILES}
    assert names == {
        "etsy-digital-listing-rules.md",
        "shopify-product-seo-checklist.md",
        "eu-vat-oss-checklist.md",
    }


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_frontmatter_is_valid_and_not_pre_approved(path):
    """Every file parses with the repo's existing frontmatter parser and declares the proposed stage only."""
    fm, body = _load(path)
    assert fm["kind"] == "skill"
    assert fm["name"].startswith("skill-")
    assert fm["status"] == "proposed"  # lifecycle respected: files never claim sandboxed/approved themselves
    assert str(fm["last_reviewed"]) == "2026-10-30"  # YAML parses an unquoted date into a date object
    known_agents = set(agent_ids())
    assert fm["candidate_agents"], "must name who the Foreman/human should eventually grant this to"
    assert set(fm["candidate_agents"]) <= known_agents
    assert body.strip()


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_body_has_required_sections(path):
    _, body = _load(path)
    assert "Sources to verify against" in body
    assert "Last reviewed" in body
    assert "2026-10-30" in body


def test_vat_skill_carries_the_not_tax_advice_disclaimer():
    _, body = _load(SKILLS_DIR / "eu-vat-oss-checklist.md")
    lowered = body.lower()
    assert "not tax advice" in lowered
    assert "accountant" in lowered
    assert "checklist" in lowered


def test_vat_skill_points_at_the_ledger_vat_table_instead_of_hardcoding_rates():
    _, body = _load(SKILLS_DIR / "eu-vat-oss-checklist.md")
    assert "VAT_RATES" in body
    assert "src/aecom/store/adapter.py" in body
    assert "vat_payable" in body
    assert "month_end.py" in body
    assert "accountant_export.py" in body
    # the checklist tells the reader to fix the rate table, not to invent a number inline
    assert "don't hardcode" in body.lower() or "do not hardcode" in body.lower()


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_no_secrets_in_skill_files(path):
    text = path.read_text()
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), f"possible secret-like string in {path.name}: {pattern.pattern}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_only_official_domains_are_referenced(path):
    text = path.read_text()
    assert not URL_RE.search(text), f"{path.name} contains a literal URL; use a bare domain name instead"
    for match in BARE_DOMAIN_RE.finditer(text):
        domain = match.group(1)
        if domain in ALLOWED_DOMAINS or any(domain.endswith(f".{d}") for d in ALLOWED_DOMAINS):
            continue
        if any(domain == f"{sub}.{d}" for d in ALLOWED_DOMAINS for sub in ("www",)):
            continue
        # filenames / module paths / version-like tokens are not domains: only flag names ending in a filename ext
        # we recognise as a public TLD-shaped reference outside the allowlist.
        if domain.endswith((".py", ".md")):
            continue
        assert False, f"{path.name} references a domain not in the allowlist: {domain}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_file_loads_through_the_real_propose_tool_registry_call(path):
    """The parsed frontmatter + body is valid input to the existing, unmodified registry lifecycle: propose_tool
    always lands at status='proposed' regardless of what the file's frontmatter says, and the skill text only
    reaches an agent after sandbox_tool -> approve_tool -> grant_tool (all human/Foreman steps)."""
    fm, body = _load(path)
    name, agent = fm["name"], fm["candidate_agents"][0]
    heading = next(line for line in body.splitlines() if line.startswith("# ")).removeprefix("# ")

    out = propose_tool(name, "skill", heading, {"instructions": body})
    assert out == {"name": name, "status": "proposed"}
    with session() as s:
        row = s.get(RegistryTool, name)
        assert row.status == "proposed"
        assert row.config["instructions"] == body
    assert name not in skills_for(agent)  # proposed only: no agent can see it yet

    with pytest.raises(ValueError, match="sandboxed first"):
        approve_tool(name)  # lifecycle enforced: cannot skip straight to approved

    sandbox_tool(name)
    with session() as s:
        assert s.get(RegistryTool, name).status == "sandboxed"
    assert name not in skills_for(agent)  # sandboxed only: still nobody

    approve_tool(name)
    with session() as s:
        assert s.get(RegistryTool, name).status == "approved"
    assert name not in skills_for(agent)  # approved but not granted: still nobody

    grant_tool(name, agent)
    granted = skills_for(agent)
    assert any(s.startswith(f"## Skill: {name}") and body in s for s in granted)


def test_registering_all_three_skills_does_not_touch_any_other_agents_allowlist():
    before = {a: set(skills_for(a)) for a in agent_ids()}
    for path in SKILL_FILES:
        fm, body = _load(path)
        propose_tool(fm["name"], "skill", fm["name"], {"instructions": body})
        sandbox_tool(fm["name"])
        approve_tool(fm["name"])
        grant_tool(fm["name"], fm["candidate_agents"][0])
    with session() as s:
        names = {fm["name"] for fm, _ in (_load(p) for p in SKILL_FILES)}
        rows = s.scalars(select(RegistryTool).where(RegistryTool.name.in_(names)))
        assert {r.status for r in rows} == {"approved"}
    for agent, prior in before.items():
        if agent in {fm["candidate_agents"][0] for fm, _ in (_load(p) for p in SKILL_FILES)}:
            continue
        assert set(skills_for(agent)) == prior  # untouched agents keep exactly what they had
