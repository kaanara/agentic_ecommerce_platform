"""ops_persona_overrides: get/save/reset per agent (and company) files, base_file_hash staleness, TOOLS.md resync."""

import sys
from pathlib import Path

import pytest

from aecom.persona import loader as pl
from aecom.persona import overrides as po
from aecom.registry.tools import allowed_tools

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # render_agents_tab does `from _common import agent_label, confirm_dialog`
sys.path.insert(0, str(DASHBOARD / "app_pages"))


@pytest.fixture(autouse=True)
def _clear_spec_cache():
    pl.load_spec.cache_clear()
    yield
    pl.load_spec.cache_clear()


def test_get_override_is_none_until_saved():
    assert po.get_override("ceo", "SOUL.md") is None
    assert po.current_text("ceo", "SOUL.md") == po.file_text("ceo", "SOUL.md")


def test_save_override_requires_a_name():
    with pytest.raises(ValueError, match="name"):
        po.save_override("ceo", "SOUL.md", "New soul.", "")


def test_save_override_rejects_unknown_scope_or_file():
    with pytest.raises(ValueError, match="does not exist"):
        po.save_override("not_an_agent", "SOUL.md", "x", "alice")
    with pytest.raises(ValueError, match="not a persona file"):
        po.save_override("ceo", "NOT_REAL.md", "x", "alice")


def test_save_and_reset_a_soul_override():
    original = po.file_text("ceo", "SOUL.md")
    po.save_override("ceo", "SOUL.md", "A completely different soul.", "alice")
    assert po.get_override("ceo", "SOUL.md") == "A completely different soul."
    assert po.current_text("ceo", "SOUL.md") == "A completely different soul."
    assert pl.load_spec("ceo").soul == "A completely different soul."

    detail = po.get_override_detail("ceo", "SOUL.md")
    assert detail["updated_by"] == "alice"
    assert detail["stale"] is False

    po.reset_override("ceo", "SOUL.md", "bob")
    assert po.get_override("ceo", "SOUL.md") is None
    assert po.current_text("ceo", "SOUL.md") == original
    assert pl.load_spec("ceo").soul == original.strip()


def test_save_override_validates_through_the_loader():
    bad_identity = """---
id: ceo
display_name: Aurelia
role: Chief Executive Officer
model_tier: not_a_real_tier
---
body
"""
    with pytest.raises(ValueError, match="would break ceo's persona"):
        po.save_override("ceo", "IDENTITY.md", bad_identity, "alice")
    assert po.get_override("ceo", "IDENTITY.md") is None  # the bad edit was never stored


def test_save_override_leaves_other_files_untouched():
    """Validating one file's edit must use the *current* effective content of the others (an existing override, if
    any), not silently fall back past it to the tracked file."""
    original_identity = po.file_text("ceo", "IDENTITY.md")
    po.save_override("ceo", "IDENTITY.md", original_identity.replace("Aurelia", "Nova"), "alice")
    pl.load_spec.cache_clear()
    assert pl.load_spec("ceo").display_name == "Nova"
    # Now edit SOUL.md; IDENTITY.md's override (Nova) must still apply.
    po.save_override("ceo", "SOUL.md", "New soul text.", "alice")
    pl.load_spec.cache_clear()
    spec = pl.load_spec("ceo")
    assert spec.display_name == "Nova"
    assert spec.soul == "New soul text."


def test_base_file_hash_warns_when_the_tracked_file_changed(tmp_path, monkeypatch):
    from aecom.config import get_settings

    settings = get_settings()
    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "ceo"
    agent_dir.mkdir(parents=True)
    for name in pl.REQUIRED_FILES:
        (agent_dir / name).write_text((settings.agents_dir / "ceo" / name).read_text())
    monkeypatch.setattr(settings, "agents_dir", agents_dir)

    po.save_override("ceo", "SOUL.md", "Overridden soul.", "alice")
    assert po.get_override_detail("ceo", "SOUL.md")["stale"] is False

    (agent_dir / "SOUL.md").write_text("The file changed underneath the override.")
    assert po.get_override_detail("ceo", "SOUL.md")["stale"] is True


def test_list_overrides_reports_every_saved_file():
    po.save_override("ceo", "SOUL.md", "x", "alice")
    po.save_override("marketing", "AGENTS.md", "y", "alice")
    rows = {(r["scope"], r["file_name"]) for r in po.list_overrides()}
    assert ("ceo", "SOUL.md") in rows
    assert ("marketing", "AGENTS.md") in rows


def test_company_scope_override_does_not_touch_persona_cache():
    po.save_override("company", "GOALS.md", "New goals.", "alice")
    assert po.current_text("company", "GOALS.md") == "New goals."
    assert pl.company_doc_text("GOALS.md") == "New goals."
    po.reset_override("company", "GOALS.md", "alice")
    assert pl.company_doc_text("GOALS.md") == po.file_text("company", "GOALS.md")


# --------------------------------------------------------------------------- TOOLS.md -> C5 allowlist resync
def test_tools_md_override_runs_the_additive_allowlist_resync():
    original_tools = po.file_text("ceo", "TOOLS.md")
    assert "get_kpis" in allowed_tools("ceo")

    # add a tool that was not previously allowlisted, so the resync has something additive to prove
    new_tools_md = original_tools.replace("recall_memory, remember", "recall_memory, remember, list_products")
    assert "list_products" not in allowed_tools("ceo")
    po.save_override("ceo", "TOOLS.md", new_tools_md, "alice")
    assert "list_products" in allowed_tools("ceo")

    po.reset_override("ceo", "TOOLS.md", "alice")
    # resetting re-runs the resync too; the additively-granted tool is never auto-revoked (C5 is additive only)
    assert "list_products" in allowed_tools("ceo")


# --------------------------------------------------------------------------- Settings > Agents tab (dashboard)
def _agents_tab_app() -> None:
    from _settings_agents import render_agents_tab

    render_agents_tab("alice")


def test_render_agents_tab_shows_scopes_and_the_tracked_file():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_agents_tab_app)
    at.run()
    assert not at.exception
    scope_box = at.selectbox(key="_agents_scope")
    assert "Company documents" in scope_box.options
    assert any("Aurelia" in o for o in scope_box.options)  # ceo's display name
    assert at.text_area[0].value == po.file_text("company", "GOALS.md")  # default scope/file


def test_render_agents_tab_saves_a_non_tools_override_directly():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_agents_tab_app)
    at.run()
    at.text_area[0].set_value("Brand new goals, edited from the dashboard.")
    at.button(key="_agents_save_company_GOALS.md").click()
    at.run()
    assert not at.exception
    assert po.get_override("company", "GOALS.md") == "Brand new goals, edited from the dashboard."


def test_render_agents_tab_shows_override_and_stale_banners(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from aecom.config import get_settings

    settings = get_settings()
    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "ceo"
    agent_dir.mkdir(parents=True)
    for name in pl.REQUIRED_FILES:
        (agent_dir / name).write_text((settings.agents_dir / "ceo" / name).read_text())
    monkeypatch.setattr(settings, "agents_dir", agents_dir)

    po.save_override("ceo", "SOUL.md", "An overridden soul.", "alice")

    def _ceo_soul_app() -> None:
        from _settings_agents import render_agents_tab

        render_agents_tab("alice")

    at = AppTest.from_function(_ceo_soul_app)
    at.run()
    at.selectbox(key="_agents_scope").set_value("🧭 Aurelia")
    at.run()
    at.selectbox(key="_agents_file_ceo").set_value("SOUL.md")
    at.run()
    assert not at.exception
    assert any("alice" in i.value for i in at.info)
    assert not at.warning  # not stale yet

    (agent_dir / "SOUL.md").write_text("The file changed underneath the override.")
    at2 = AppTest.from_function(_ceo_soul_app)
    at2.run()
    at2.selectbox(key="_agents_scope").set_value("🧭 Aurelia")
    at2.run()
    at2.selectbox(key="_agents_file_ceo").set_value("SOUL.md")
    at2.run()
    assert not at2.exception
    assert any("changed since this override" in w.value for w in at2.warning)


def test_render_agents_tab_opens_a_confirm_dialog_for_tools_md():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_agents_tab_app)
    at.run()
    at.selectbox(key="_agents_scope").set_value("🧭 Aurelia")
    at.run()
    at.selectbox(key="_agents_file_ceo").set_value("TOOLS.md")
    at.run()
    at.button(key="_agents_save_ceo_TOOLS.md").click()
    at.run()
    assert not at.exception  # opens the confirm dialog rather than saving straight away
    assert po.get_override("ceo", "TOOLS.md") is None
