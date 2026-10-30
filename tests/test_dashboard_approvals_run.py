"""AppTest smoke tests for dashboard/app_pages/approvals.py and run_detail.py (W3 builder H1)."""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from aecom import conversations
from aecom import dashboard_auth as auth
from aecom.company_settings import set_setting
from aecom.db import Approval, session

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the pages do `from _common import ...`

HOME = str(DASHBOARD / "Home.py")
APPROVALS_PAGE = "app_pages/approvals.py"
RUN_DETAIL_PAGE = "app_pages/run_detail.py"


def _app(login_off: bool = True) -> AppTest:
    if login_off:
        set_setting(auth.MODE_KEY, "off")
    else:
        auth.reset_password_local("a long test password")
    return AppTest.from_file(HOME, default_timeout=60)


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def _park(agent_id: str = "cfo", amount: float = 250.0) -> str:
    with session() as s:
        a = Approval(agent_id=agent_id, tool_name="record_expense",
                     args={"account": "vendor_expense", "amount_eur": amount, "memo": "m"},
                     risk_tier="T2", reason="needs a human", context="support ticket #1")
        s.add(a)
        s.flush()
        return a.id


def test_approvals_page_renders_with_no_pending():
    at = _app().run()
    at = at.switch_page(APPROVALS_PAGE).run()
    assert not at.exception, _messages(at)
    assert at.title and "approvals" in at.title[0].value.lower()
    assert any("Nothing waiting" in m.value for m in at.markdown)


def test_approvals_card_renders_and_reviewer_name_is_shared_from_the_sidebar():
    _park()
    at = _app().run()
    at.sidebar.text_input(key="reviewer_name").set_value("kaan")
    at = at.switch_page(APPROVALS_PAGE).run()
    assert not at.exception, _messages(at)
    assert any("record_expense" in m.value for m in at.markdown)
    buttons = {b.key: b for b in at.button}
    approve = next(b for k, b in buttons.items() if k and k.startswith("ok_"))
    assert approve.disabled is False  # reviewer name set, so the control is enabled


def test_approvals_approve_runs_edit_then_approve_flow():
    apr_id = _park(amount=300.0)
    at = _app().run()
    at.sidebar.text_input(key="reviewer_name").set_value("kaan")
    at = at.switch_page(APPROVALS_PAGE).run()
    at.text_area(key=f"args_{apr_id}").set_value('{"account": "vendor_expense", "amount_eur": 200.0, "memo": "m"}')
    at.button(key=f"ok_{apr_id}").click().run()
    assert not at.exception, _messages(at)
    with session() as s:
        a = s.get(Approval, apr_id)
        assert a.status == "executed" and a.args_edited is True and a.args["amount_eur"] == 200.0


def test_run_detail_renders_with_no_runs():
    at = _app().run()
    at = at.switch_page(RUN_DETAIL_PAGE).run()
    assert not at.exception, _messages(at)
    assert at.title and "run detail" in at.title[0].value.lower()
    assert any("No agent runs yet" in i.value for i in at.info)


def test_run_detail_links_back_to_the_chat_conversation_for_a_chat_run():
    conv_id = conversations.create("ceo", "owner")
    user_msg_id = conversations.append_message(conv_id, "user", "hello")
    run_id = f"chat-{user_msg_id}"
    conversations.append_message(conv_id, "assistant", "hi there", agent_id="ceo", agent_run_id=run_id)

    at = _app().run()
    at = at.switch_page(RUN_DETAIL_PAGE).run()
    at.query_params["run_id"] = run_id
    at.run()
    assert not at.exception, _messages(at)
    assert any(f"/chat?conversation={conv_id}" in m.value for m in at.markdown)
