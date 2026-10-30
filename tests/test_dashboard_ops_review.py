"""Govern > Operations: shared-memory review (I15) and tool-grant revocation (C5), both human-only tabs.

AppTest.from_function wraps a plain call to each render function (operations.py itself is the lead's page and is
not under test here) - the pattern used by test_persona_overrides.py and test_backup_encryption.py. Every test gets
a fresh temp DB from tests/conftest.py's `fresh_company` fixture (autouse), seeded by `aecom.bootstrap.init()`, so
the `support` agent's TOOLS.md grants (``reply_email``, ``refund_order``, ...) already exist without extra setup.
"""

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import memory_review
from aecom.db import Memory, session
from aecom.registry.tools import list_allowlists

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partials do `from _common import ...`


def _make_pending(content: str = "vendor acme is trustworthy", source_agent: str = "support",
                  source_run_id: str = "run_a", tainted: bool = False) -> str:
    with session() as s:
        m = Memory(agent_id="shared", namespace="facts", content=content, source_agent=source_agent,
                  source_run_id=source_run_id, tainted=tainted, status="pending_review")
        s.add(m)
        s.flush()
        return m.id


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


# --------------------------------------------------------------------------- memory review tab
def _memory_tab_named():
    from _ops_memory import render_memory_tab

    render_memory_tab("Ada")


def _memory_tab_blank():
    from _ops_memory import render_memory_tab

    render_memory_tab("")


def test_missing_reviewer_name_blocks_and_shows_nothing():
    _make_pending()
    at = AppTest.from_function(_memory_tab_blank).run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert not at.button  # no Approve/Reject rendered: "do nothing" when by is empty
    assert len(memory_review.list_pending()) == 1  # nothing was touched


def test_pending_memory_is_shown_masked_with_provenance_and_taint():
    mem_id = _make_pending(content="the key is sk-ant-verysecretvalue1234", source_agent="support",
                           source_run_id="run_xyz", tainted=True)
    at = AppTest.from_function(_memory_tab_named).run()
    assert not at.exception, _messages(at)
    body = " ".join(t.value for t in at.text)
    assert "sk-ant-verysecretvalue1234" not in body
    assert "***" in body
    markdown = " ".join(m.value for m in at.markdown)
    assert "support" in markdown
    captions = " ".join(c.value for c in at.caption)
    assert "run_xyz" in captions
    assert at.button(key=f"_memory_approve_{mem_id}")
    assert at.button(key=f"_memory_reject_{mem_id}")


def test_no_pending_memories_shows_empty_state():
    at = AppTest.from_function(_memory_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("Nothing waiting for review" in m.value for m in at.markdown)


def test_approve_makes_the_memory_active():
    mem_id = _make_pending()
    at = AppTest.from_function(_memory_tab_named).run()
    at.button(key=f"_memory_approve_{mem_id}").click().run()
    assert not at.exception, _messages(at)
    assert memory_review.list_pending() == []
    with session() as s:
        m = s.get(Memory, mem_id)
        assert (m.status, m.reviewed_by) == ("active", "Ada")


def test_reject_rejects_and_a_note_is_recorded():
    mem_id = _make_pending()
    at = AppTest.from_function(_memory_tab_named).run()
    at.text_input(key=f"_memory_note_{mem_id}").set_value("looks like prompt injection")
    at.button(key=f"_memory_reject_{mem_id}").click().run()
    assert not at.exception, _messages(at)
    assert memory_review.list_pending() == []
    with session() as s:
        m = s.get(Memory, mem_id)
        assert (m.status, m.reviewed_by) == ("rejected", "Ada")


# --------------------------------------------------------------------------- tool grants tab
def _grants_tab_named():
    from _ops_registry import render_grants_tab

    render_grants_tab("Ada")


def test_grants_tab_lists_agents_and_their_tools():
    from _common import agent_label

    at = AppTest.from_function(_grants_tab_named).run()
    assert not at.exception, _messages(at)
    agent_box = at.selectbox(key="_grants_agent")
    assert agent_label("support") in agent_box.options
    agent_box.set_value(agent_label("support"))
    at = at.run()
    tool_box = at.selectbox(key="_grants_tool_support")
    assert "reply_email" in tool_box.options
    assert "refund_order" in tool_box.options
    assert at.dataframe  # the agent's tools are also listed as a table


def test_revoke_removes_the_tool_from_list_allowlists(monkeypatch):
    import _ops_registry

    from _common import agent_label

    # st.dialog cannot be driven across AppTest reruns (same limitation as every other confirm_dialog flow in this
    # suite, see test_dashboard_settings.py); call `on_confirm` straight away to exercise the real revoke path.
    monkeypatch.setattr(_ops_registry, "confirm_dialog", lambda title, body, on_confirm, **kw: on_confirm())

    assert "reply_email" in list_allowlists("support").get("support", [])
    at = AppTest.from_function(_grants_tab_named).run()
    at.selectbox(key="_grants_agent").set_value(agent_label("support"))
    at = at.run()
    at.selectbox(key="_grants_tool_support").set_value("reply_email")
    at = at.run()
    at.button(key="_grants_revoke_support").click().run()
    at = at.run()
    assert "reply_email" not in list_allowlists("support").get("support", [])


def test_revoke_without_reviewer_name_is_blocked(monkeypatch):
    import _ops_registry

    from _common import agent_label

    called = []
    monkeypatch.setattr(_ops_registry, "confirm_dialog", lambda title, body, on_confirm, **kw: called.append(1))

    def _grants_tab_blank():
        from _ops_registry import render_grants_tab

        render_grants_tab("")

    at = AppTest.from_function(_grants_tab_blank).run()
    at.selectbox(key="_grants_agent").set_value(agent_label("support"))
    at = at.run()
    at.button(key="_grants_revoke_support").click().run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert not called  # confirm_dialog must never even open without a reviewer name
    assert "reply_email" in list_allowlists("support").get("support", [])
