"""Operations tab actions (aecom.ops_actions): a company run, store catalog sync, reconcile, monitors, and the
AgentMail poll, each run exactly as the scheduler runs them (``jobs.run_pending``). Offline throughout: the
``fresh_company`` fixture (tests/conftest.py) gives every test its own tmp SQLite DB and ``LLM_MODE=scripted``;
nothing here makes a real network or LLM call.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from aecom import dashboard_auth as auth
from aecom import db, jobs, ops_actions
from aecom.bootstrap import seed_demo
from aecom.bus.events import drain
from aecom.company_settings import set_setting
from aecom.db import AuditLog, EventRow, Order, session
from aecom.store.adapter import get_store

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the page does `from _common import ...`
HOME = str(DASHBOARD / "Home.py")
OPERATIONS_PAGE = "app_pages/operations.py"
SKU = "LIC-FOCUSFLOW-PRO-V1"


@pytest.fixture(autouse=True)
def _restore_handlers():
    """aecom.ops_actions registers its handlers once at import time (like aecom.backup's _register); this just
    isolates jobs._handlers per test, the same guard test_jobs.py uses, in case another test file registers a
    fake handler for one of these kinds."""
    saved = dict(jobs._handlers)
    yield
    jobs._handlers.clear()
    jobs._handlers.update(saved)


# --- request() validation --------------------------------------------------------------------------------------

def test_request_rejects_an_empty_directive():
    with pytest.raises(ValueError, match=r"1-2000"):
        ops_actions.request("run", "kaan", directive="   ")


def test_request_rejects_a_directive_over_2000_characters():
    with pytest.raises(ValueError, match=r"1-2000"):
        ops_actions.request("run", "kaan", directive="x" * 2001)


def test_request_queues_the_job_and_audits_it():
    jid = ops_actions.request("run", "kaan", directive="Say hello")
    j = jobs.get(jid)
    assert j["status"] == "queued" and j["params"] == {"directive": "Say hello"} and j["requested_by"] == "kaan"
    with session() as s:
        row = s.scalars(select(AuditLog).where(AuditLog.action == "job_run_requested")).first()
        assert row is not None and row.actor == "human:kaan" and row.detail["job_id"] == jid


def test_request_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="unknown"):
        ops_actions.request("rm-rf", "kaan")


# --- run: one company run ---------------------------------------------------------------------------------------

def test_run_company_job_runs_through_run_pending():
    jid = ops_actions.request("run", "kaan", directive="Say hello")
    assert jobs.run_pending() == [jid]
    j = jobs.get(jid)
    assert j["status"] == "ok"
    result = j["result"]
    assert set(result) == {"run_id", "plan", "verdict", "summary", "pending_approvals"}
    assert result["run_id"] and isinstance(result["pending_approvals"], list)


def test_run_company_job_raises_without_a_directive():
    with pytest.raises(ValueError, match="directive"):
        ops_actions.run_company_job({})


# --- sync_store ----------------------------------------------------------------------------------------------

def test_sync_store_job_runs_the_adapters_sync_catalog(monkeypatch):
    class FakeStore:
        name = "fake"

        def sync_catalog(self):
            return {"created": 2, "updated": 1}

    monkeypatch.setattr("aecom.store.adapter.get_store", lambda: FakeStore())
    jid = jobs.enqueue("sync_store", "kaan")
    jobs.run_pending()
    j = jobs.get(jid)
    assert j["status"] == "ok" and j["result"] == {"created": 2, "updated": 1}


def test_sync_store_job_errors_for_an_adapter_without_a_catalog():
    """Default STORE_ADAPTER=mock has no sync_catalog (only lemonsqueezy does, same check as `aecom store sync`)."""
    assert get_store().name == "mock"
    jid = jobs.enqueue("sync_store", "kaan")
    jobs.run_pending()
    j = jobs.get(jid)
    assert j["status"] == "error" and "no catalog to sync" in j["result"]["error"]


# --- reconcile -------------------------------------------------------------------------------------------------

def test_reconcile_job_requeues_a_stuck_order_and_drains_it():
    seed_demo()
    drain()  # settle whatever seeding queued
    order = get_store().create_order("lost@example.com", SKU)
    with session() as s:  # simulate a lost delivery event, aged past the cutoff
        for e in s.scalars(select(EventRow).where(EventRow.topic == "order.paid")):
            if e.payload.get("order_id") == order["id"]:
                e.processed = True
        s.get(Order, order["id"]).created_at = db.now() - timedelta(minutes=30)

    jid = ops_actions.request("reconcile", "kaan", older_than_minutes=15)
    jobs.run_pending()
    j = jobs.get(jid)
    assert j["status"] == "ok"
    assert j["result"]["requeued"] == 1
    assert j["result"]["events_drained"] >= 1
    assert get_store().get_order(order["id"])["status"] == "delivered"


def test_reconcile_job_defaults_to_15_minutes():
    jid = jobs.enqueue("reconcile", "kaan", {})
    jobs.run_pending()
    j = jobs.get(jid)
    assert j["status"] == "ok" and j["result"]["checked"] == 0


# --- monitors --------------------------------------------------------------------------------------------------

def test_monitors_job_runs_evaluate_and_returns_a_summary():
    jid = jobs.enqueue("monitors", "kaan")
    jobs.run_pending()
    j = jobs.get(jid)
    assert j["status"] == "ok"
    assert {"failing", "alerted", "open", "heartbeat"} <= set(j["result"])
    assert j["result"]["heartbeat"] is False  # no HEALTHCHECKS_PING_URL configured -> no network call


# --- agentmail_poll --------------------------------------------------------------------------------------------

def test_agentmail_poll_job_is_a_no_op_without_an_api_key():
    jid = jobs.enqueue("agentmail_poll", "kaan")
    jobs.run_pending()
    j = jobs.get(jid)
    assert j["status"] == "ok" and j["result"] == {"new_messages": 0, "events_drained": 0}


# --- the dashboard tab (AppTest) --------------------------------------------------------------------------------

def _app() -> AppTest:
    set_setting(auth.MODE_KEY, "off")
    at = AppTest.from_file(HOME, default_timeout=60)
    at.run()
    at.sidebar.text_input(key="reviewer_name").set_value("kaan")
    at.switch_page(OPERATIONS_PAGE)
    return at.run()


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def test_operations_tab_renders_and_shows_the_llm_mode_warning():
    at = _app()
    assert not at.exception, _messages(at)
    assert at.tabs and at.tabs[0].label == "Run and maintenance"
    assert any("LLM_MODE=scripted" in c.value for t in at.tabs for c in t.get("caption"))


def test_operations_tab_start_company_run_button_queues_a_run_job():
    at = _app()
    at.text_area(key="_ops_run_directive").set_value("Say hello from the dashboard")
    at = at.button(key="_ops_run_btn").click().run()
    assert not at.exception, _messages(at)
    assert any("Queued" in m.value for m in at.success)
    rows = jobs.list_jobs("run")
    assert rows and rows[0]["status"] == "queued" and rows[0]["params"]["directive"] == "Say hello from the dashboard"
    assert rows[0]["requested_by"] == "kaan"


def test_operations_tab_sync_store_button_queues_a_sync_store_job():
    at = _app()
    at = at.button(key="_ops_sync_btn").click().run()
    assert not at.exception, _messages(at)
    rows = jobs.list_jobs("sync_store")
    assert rows and rows[0]["status"] == "queued" and rows[0]["requested_by"] == "kaan"


def test_operations_tab_reconcile_button_uses_the_entered_minutes():
    at = _app()
    at.number_input(key="_ops_reconcile_minutes").set_value(42)
    at = at.button(key="_ops_reconcile_btn").click().run()
    assert not at.exception, _messages(at)
    rows = jobs.list_jobs("reconcile")
    assert rows and rows[0]["params"]["older_than_minutes"] == 42


def test_operations_tab_requires_a_name_before_queuing():
    set_setting(auth.MODE_KEY, "off")
    at = AppTest.from_file(HOME, default_timeout=60)
    at.run()
    at.switch_page(OPERATIONS_PAGE)
    at = at.run()
    at = at.button(key="_ops_monitors_btn").click().run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in m.value for m in at.warning)
    assert jobs.list_jobs("monitors") == []
