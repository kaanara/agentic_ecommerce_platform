"""E1/E4/E7: the provisioning loop - need → CEO → human → credential → active → Foreman."""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import select

from aecom import approvals, provisioning
from aecom.bus import events
from aecom.bus.events import drain
from aecom.company_settings import set_agent_enabled
from aecom.db import AgentRun, AuditLog, EventRow, Incident, Memory, Vendor, now, session
from aecom.graph.agents import AgentResult
from aecom.handlers import invoke_as, run_sync
from aecom.llm import Script
from aecom.persona.loader import agent_ids, load_spec
from aecom.policy.gate import evaluate, gate_builtin
from aecom.scenarios import final

REQUEST = {"vendor": "Acme Ads", "category": "ads", "purpose": "Paid search",
           "steps": ["Create the Acme Ads account", "Create an API token", "Put it in .env as ACME_ADS_TOKEN"],
           "credential_env": "ACME_ADS_TOKEN", "monthly_cost_eur": 0.0}


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(events, "BACKOFF", [timedelta(0)] * 5)


def request(**override) -> str:
    """The CEO asks; the T3 gate parks it. Returns the approval id."""
    out = run_sync(gate_builtin("request_provisioning", "ceo").ainvoke({**REQUEST, **override}))
    assert out.startswith("ESCALATED"), out
    return out.split("id=")[1].split(")")[0]


def vendor(name: str = "Acme Ads") -> Vendor:
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == name)).one()
        s.expunge(v)
        return v


def runs(agent_id: str) -> list[AgentRun]:
    with session() as s:
        return list(s.scalars(select(AgentRun).where(AgentRun.agent_id == agent_id)))


def make_provisioning() -> None:
    # activation="auto": these tests are about the activation hand-off; the default for unknown vendors (human_confirm,
    # U3) is covered in test_provisioning_flow.py
    approvals.approve(request(activation="auto", activation_reason="test vendor"), "tester")
    drain()


# ----------------------------------------------------------------------------- approval → checklist / rejection
def test_approved_request_sets_the_checklist():
    make_provisioning()
    v = vendor()
    assert v.status == "provisioning" and v.credential_env == "ACME_ADS_TOKEN"
    assert v.checklist == [{"step": s, "done": False} for s in REQUEST["steps"]]


def test_rejected_request_marks_vendor_rejected_and_tells_the_ceo():
    approvals.reject(request(), "tester", "Too expensive for now")
    drain()
    v = vendor()
    assert v.status == "rejected" and "Too expensive for now" in v.notes
    with session() as s:
        mem = [m.content for m in s.scalars(select(Memory).where(Memory.agent_id == "ceo",
                                                                  Memory.namespace == "provisioning"))]
    assert mem == ["Rejected Acme Ads: Too expensive for now"]

    # a later approved request reports the stored reason
    out = approvals.approve(request(), "tester")
    assert out["status"] == "executed" and "Too expensive for now" in out["result"]
    drain()
    assert vendor().status == "provisioning" and len(vendor().checklist) == 3


def test_second_request_for_an_active_vendor_does_not_reset_it():
    make_provisioning()
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == "Acme Ads")).one()
        v.status, v.verified_at = "active", now()
        v.checklist = [{"step": "done already", "done": True}]
    out = approvals.approve(request(credential_env="OTHER_TOKEN", steps=["Sign up again"]), "tester")
    assert '"unchanged": true' in out["result"] and "already active" in out["result"]
    drain()
    v = vendor()
    assert v.status == "active" and v.credential_env == "ACME_ADS_TOKEN" and v.verified_at is not None
    assert v.checklist == [{"step": "done already", "done": True}]


def test_rejecting_a_duplicate_does_not_undo_a_provisioning_vendor():
    make_provisioning()
    approvals.reject(request(), "tester", "duplicate")
    drain()
    assert vendor().status == "provisioning"


# ----------------------------------------------------------------------------- credential arrives
def test_check_credentials_activates_and_gives_the_foreman_a_task(monkeypatch):
    make_provisioning()
    assert provisioning.check_credentials()["activated"] == []
    assert vendor().status == "provisioning" and "ACME_ADS_TOKEN" in vendor().last_check_error

    monkeypatch.setattr(provisioning.secrets, "has", lambda name: name == "ACME_ADS_TOKEN")
    Script.load({"foreman": [final("Proposed the Acme Ads MCP server.")]})
    assert provisioning.check_credentials() == {"checked": 1, "activated": ["Acme Ads"]}
    v = vendor()
    assert v.status == "active" and v.verified_at is not None
    assert v.last_check_error.startswith("warning:")  # E6: no verifier for Acme Ads, presence only
    foreman = runs("foreman")
    assert len(foreman) == 1 and "Acme Ads is provisioned" in foreman[0].task
    with session() as s:
        evt = s.scalars(select(EventRow).where(EventRow.topic == "vendor.provisioned")).one()
        assert evt.payload["vendor"] == "Acme Ads"
        assert s.scalars(select(Incident).where(Incident.monitor == "provisioning_followup")).all() == []
    assert provisioning.check_credentials() == {"checked": 0, "activated": []}  # once only


def test_disabled_foreman_raises_a_p3_followup_incident(monkeypatch):
    make_provisioning()
    monkeypatch.setattr(provisioning.secrets, "has", lambda name: True)
    set_agent_enabled("foreman", False)
    assert provisioning.check_credentials()["activated"] == ["Acme Ads"]
    assert vendor().status == "active"
    with session() as s:
        inc = s.scalars(select(Incident).where(Incident.monitor == "provisioning_followup")).one()
    assert (inc.monitor, inc.severity, inc.subject) == ("provisioning_followup", "P3", "Acme Ads")


def test_the_credential_value_is_never_logged(monkeypatch, caplog):
    secret = "acme-live-7f3c9e1d5b"
    make_provisioning()
    monkeypatch.setenv("ACME_ADS_TOKEN", secret)
    Script.load({"foreman": [final("ok")]})
    with caplog.at_level(logging.DEBUG):
        assert provisioning.check_credentials()["activated"] == ["Acme Ads"]
        drain()
    assert secret not in caplog.text
    with session() as s:
        stored = [str(a.detail) for a in s.scalars(select(AuditLog))]
        stored += [str(e.payload) for e in s.scalars(select(EventRow))]
        stored += [r.task + (r.output or "") for r in s.scalars(select(AgentRun))]
        v = s.scalars(select(Vendor)).one()
        stored += [v.notes, str(v.checklist), str(v.last_check_error)]
    assert not any(secret in x for x in stored)


# ----------------------------------------------------------------------------- account needs (E4)
def test_every_agent_has_request_account_need_at_t1():
    for agent_id in set(agent_ids()) - {"sentinel"}:  # the Sentinel is strictly read-only
        assert "request_account_need" in load_spec(agent_id).tools
        assert evaluate(agent_id, "request_account_need", {"vendor": "x", "why": "y"}).decision == "allow"


def test_account_need_from_marketing_gets_one_ceo_run_per_vendor_per_day(monkeypatch):
    out = invoke_as("marketing", "request_account_need",
                    {"vendor": "TikTok Ads", "why": "Our audience is on TikTok", "evidence": "brief res_1"})
    assert "Recorded; the CEO decides" in out
    invoke_as("support", "request_account_need", {"vendor": "tiktok ads", "why": "Customers ask about it"})
    with session() as s:
        evt = s.scalars(select(EventRow).where(EventRow.topic == "account.need_raised")).first()
    assert evt.payload == {"vendor": "TikTok Ads", "why": "Our audience is on TikTok", "evidence": "brief res_1",
                           "raised_by": "marketing"}
    drain()
    ceo = runs("ceo")
    assert len(ceo) == 1 and "TikTok Ads" in ceo[0].task and "request_provisioning" in ceo[0].task

    invoke_as("marketing", "request_account_need", {"vendor": "Pinterest Ads", "why": "Visual products"})
    drain()
    assert len(runs("ceo")) == 2

    tomorrow = now() + timedelta(days=1)
    monkeypatch.setattr(provisioning, "now", lambda: tomorrow)
    invoke_as("marketing", "request_account_need", {"vendor": "TikTok Ads", "why": "Still needed"})
    drain()
    assert len(runs("ceo")) == 3


def test_failed_ceo_run_is_retried_by_the_bus(monkeypatch, no_backoff):
    calls: list[str] = []

    async def failing(agent_id, task, **kw):
        calls.append(agent_id)
        return AgentResult(agent_id, "boom", status="error")

    monkeypatch.setattr("aecom.graph.agents.run_agent", failing)
    invoke_as("marketing", "request_account_need", {"vendor": "TikTok Ads", "why": "reach"})
    drain()
    assert len(calls) == events.MAX_ATTEMPTS  # every attempt re-ran the CEO: nothing marked it decided
    with session() as s:
        evt = s.scalars(select(EventRow).where(EventRow.topic == "account.need_raised")).one()
        assert evt.dead and "ceo error" in evt.last_error


def test_agents_still_cannot_sign_up():
    assert evaluate("marketing", "mcp:tiktok/create_account", {}).decision == "deny"
    assert evaluate("ceo", "mcp:acme/sign_up", {}).decision == "deny"
