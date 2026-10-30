"""persona.prompt.company_context() and tools.catalog.read_company_doc both read company/*.md through
aecom.persona.loader.company_doc_text(), which prefers a UI override (aecom.persona.overrides) over the file
in git. Offline: temp DATABASE_URL (conftest.py), LLM_MODE=scripted, no network."""

from __future__ import annotations

from aecom.persona import overrides as po
from aecom.persona.prompt import company_context
from aecom.tools.catalog import read_company_doc


def test_company_context_falls_back_to_the_tracked_file():
    assert po.get_override("company", "GOALS.md") is None
    text = company_context()
    assert "digital goods" in text
    assert "owner: ceo" not in text  # frontmatter is stripped


def test_company_context_uses_a_saved_override():
    po.save_override("company", "GOALS.md", "New strategic goals for this quarter.", "alice")
    assert company_context() == "New strategic goals for this quarter."
    po.reset_override("company", "GOALS.md", "alice")
    assert "digital goods" in company_context()


def test_read_company_doc_falls_back_to_the_tracked_file():
    assert po.get_override("company", "GOALS.md") is None
    text = read_company_doc("GOALS")
    assert "digital goods" in text
    assert "owner: ceo" in text  # catalog tool returns the raw file (frontmatter included), as before


def test_read_company_doc_uses_a_saved_override():
    po.save_override("company", "GOALS.md", "Overridden goals doc.", "alice")
    assert read_company_doc("GOALS") == "Overridden goals doc."
    assert read_company_doc("goals") == "Overridden goals doc."  # name is upper-cased
    po.reset_override("company", "GOALS.md", "alice")
    assert "Overridden goals doc." not in read_company_doc("GOALS")


def test_read_company_doc_unknown_name_is_unaffected_by_overrides():
    assert read_company_doc("NOT_REAL") == "unknown document NOT_REAL"
    po.save_override("company", "GOALS.md", "Some goals.", "alice")
    assert read_company_doc("NOT_REAL") == "unknown document NOT_REAL"
