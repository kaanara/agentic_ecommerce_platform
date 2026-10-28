"""E5/E6: the human-friendly provisioning flow (action-needed email, P3 incident, reminders, checklist CLI) and
real credential verification (one read-only call per vendor; the credential value never leaves the call)."""

import json
import logging
from datetime import timedelta

import httpx
import pytest
from aecom import approvals, provisioning
from aecom.bus.events import drain
from aecom.cli import main
from aecom.db import AgentRun, Approval, AuditLog, ErrorRow, EventRow, Incident, OutboxEmail, Vendor, now, session
from aecom.handlers import run_sync
from aecom.llm import Script
from aecom.policy.gate import gate_builtin
from aecom.scenarios import final
from sqlalchemy import select

ALERT_TO = "owner@example.com"
REQUEST = {"vendor": "Acme Ads", "category": "ads", "purpose": "Paid search",
           "steps": ["Create the Acme Ads account", "Create an API token with read-only reporting scope",
                     "Put it in .env as ACME_ADS_TOKEN"],
           "credential_env": "ACME_ADS_TOKEN", "monthly_cost_eur": 12.0}
LS = {"vendor": "Lemon Squeezy", "category": "platform", "purpose": "Storefront",
      "steps": ["Create a Lemon Squeezy API key", "Put it in .env as LEMONSQUEEZY_API_KEY"],
      "credential_env": "LEMONSQUEEZY_API_KEY", "monthly_cost_eur": 0.0}
AM = {"vendor": "AgentMail", "category": "email", "purpose": "Email",
      "steps": ["Create an inbox-scoped key with inbox_read, message_send permissions",
                "Put it in .env as AGENTMAIL_API_KEY"],
      "credential_env": "AGENTMAIL_API_KEY", "monthly_cost_eur": 0.0}
SECRET = "cred-value-9a8b7c6d5e4f"


@pytest.fixture(autouse=True)
def alert_email(monkeypatch):
    monkeypatch.setenv("ALERT_EMAIL", ALERT_TO)
    Script.load({"foreman": [final("Integration proposed.")] * 3})


class FakeHttp:
    """Stands in for httpx.get: answers by URL suffix, records every call."""

    def __init__(self, answers: dict[str, int | Exception]):
        self.answers, self.calls = answers, []

    def __call__(self, url, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        for suffix, answer in self.answers.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return httpx.Response(answer, request=httpx.Request("GET", url), json={})
        raise AssertionError(f"unexpected call {url}")


def provision(req: dict = REQUEST) -> str:
    out = run_sync(gate_builtin("request_provisioning", "ceo").ainvoke(req))
    assert out.startswith("ESCALATED"), out
    approval_id = out.split("id=")[1].split(")")[0]
    approvals.approve(approval_id, "tester")
    drain()
    return approval_id


def vendor(name: str = "Acme Ads") -> Vendor:
    with session() as s:
        v = s.scalars(select(Vendor).where(Vendor.name == name)).one()
        s.expunge(v)
        return v


def emails(subject_part: str = "") -> list[OutboxEmail]:
    with session() as s:
        rows = list(s.scalars(select(OutboxEmail).where(OutboxEmail.direction == "out").order_by(OutboxEmail.id)))
        for r in rows:
            s.expunge(r)
    return [r for r in rows if subject_part in r.subject]


def incidents(monitor: str = "provisioning_action") -> list[Incident]:
    with session() as s:
        rows = list(s.scalars(select(Incident).where(Incident.monitor == monitor)))
        for r in rows:
            s.expunge(r)
        return rows


def audits(decision: str) -> list[AuditLog]:
    with session() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "provisioning", AuditLog.decision == decision)))
        for r in rows:
            s.expunge(r)
        return rows


# ----------------------------------------------------------------------------- E5: action needed
def test_executed_approval_emails_the_human_and_raises_a_p3_incident():
    approval_id = provision()
    [mail] = emails("[action needed]")
    assert mail.to == ALERT_TO and "Acme Ads" in mail.subject
    for part in ("Vendor: Acme Ads", "Purpose: Paid search", "Monthly cost: EUR 12.00", "1. [ ] Create the Acme Ads account",
                 "3. [ ] Put it in .env as ACME_ADS_TOKEN", "ACME_ADS_TOKEN",
                 "- Create an API token with read-only reporting scope", "aecom provisioning done"):
        assert part in mail.body, part
    [inc] = incidents()
    assert (inc.severity, inc.subject, inc.status) == ("P3", "Acme Ads", "open") and approval_id in inc.detail

    provisioning.on_approval_resolved({"tool": "request_provisioning", "status": "executed",
                                       "approval_id": approval_id})  # a redelivered event changes nothing
    assert len(emails("[action needed]")) == 1 and len(incidents()) == 1


def test_without_alert_email_the_incident_is_still_raised(monkeypatch):
    monkeypatch.delenv("ALERT_EMAIL")
    provision()
    assert emails() == [] and len(incidents()) == 1


# ----------------------------------------------------------------------------- E5: reminders
def test_reminder_after_three_days_not_before_and_snooze_defers_it():
    provision()
    t0 = now()
    assert provisioning.check_credentials(now=t0 + timedelta(days=2, hours=23)).get("reminded") is None
    assert emails("[reminder]") == []

    t1 = t0 + timedelta(days=3, minutes=1)
    assert provisioning.check_credentials(now=t1)["reminded"] == ["Acme Ads"]
    [mail] = emails("[reminder]")
    assert "still waiting" in mail.subject and "ACME_ADS_TOKEN is not set" in mail.body and "1. [ ]" in mail.body
    assert vendor().last_reminded_at == t1

    assert "reminded" not in provisioning.check_credentials(now=t1 + timedelta(days=2))  # at most every 3 days
    t2 = t1 + timedelta(days=3, minutes=1)
    assert provisioning.check_credentials(now=t2)["reminded"] == ["Acme Ads"]
    assert len(emails("[reminder]")) == 2

    out = provisioning.snooze("acme ads", 7, by="kaan", now=t2)
    assert vendor().last_reminded_at == t2 + timedelta(days=7) and out["vendor"] == "Acme Ads"
    assert "reminded" not in provisioning.check_credentials(now=t2 + timedelta(days=9))
    assert provisioning.check_credentials(now=t2 + timedelta(days=10, minutes=1))["reminded"] == ["Acme Ads"]
    assert audits("snoozed")[0].actor == "human:kaan"


# ----------------------------------------------------------------------------- E5: checklist CLI
def test_cli_list_done_snooze(capsys):
    provision()
    capsys.readouterr()
    main(["provisioning"])
    [row] = json.loads(capsys.readouterr().out)
    assert row["vendor"] == "Acme Ads" and row["credential_env"] == "ACME_ADS_TOKEN"
    assert [c["no"] for c in row["checklist"]] == [1, 2, 3] and not any(c["done"] for c in row["checklist"])

    main(["provisioning", "done", "Acme Ads", "2", "--by", "kaan"])
    assert json.loads(capsys.readouterr().out)["remaining"] == 2
    assert [c["done"] for c in vendor().checklist] == [False, True, False]
    [a] = audits("step_done")
    assert a.actor == "human:kaan" and a.detail == {"vendor": "Acme Ads", "step": 2}

    with pytest.raises(SystemExit):
        main(["provisioning", "done", "Acme Ads", "9", "--by", "kaan"])
    with pytest.raises(SystemExit):
        main(["provisioning", "done", "Acme Ads", "1"])  # --by is required
    with pytest.raises(SystemExit):
        main(["provisioning", "done", "Nope", "1", "--by", "kaan"])

    before = now()
    main(["provisioning", "snooze", "Acme Ads", "--days", "5"])
    assert vendor().last_reminded_at >= before + timedelta(days=5)


# ----------------------------------------------------------------------------- E6: verifiers
def test_lemonsqueezy_verifier_ok(monkeypatch):
    provision(LS)
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", SECRET)
    http = FakeHttp({"/v1/users/me": 200})
    monkeypatch.setattr(httpx, "get", http)
    assert provisioning.check_credentials()["activated"] == ["Lemon Squeezy"]
    [call] = http.calls
    assert call["url"] == "https://api.lemonsqueezy.com/v1/users/me" and call["timeout"]
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    v = vendor("Lemon Squeezy")
    assert v.status == "active" and v.verified_at is not None and v.last_check_error is None
    assert emails("is active")
    # the action-needed incident resolves when the vendor becomes active
    assert [i.status for i in incidents()] == ["resolved"]


def test_lemonsqueezy_401_keeps_provisioning_and_emails_the_error_once(monkeypatch):
    provision(LS)
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", SECRET)
    monkeypatch.setattr(httpx, "get", FakeHttp({"/v1/users/me": 401}))
    assert provisioning.check_credentials()["activated"] == []
    v = vendor("Lemon Squeezy")
    assert v.status == "provisioning" and "HTTP 401" in v.last_check_error
    [mail] = emails("does not work")
    assert "HTTP 401" in mail.body and "LEMONSQUEEZY_API_KEY" in mail.body
    provisioning.check_credentials()  # same error again: no second email
    assert len(emails("does not work")) == 1
    assert [i.status for i in incidents()] == ["open"]


def test_agentmail_verifier_ok_and_warns_about_an_org_wide_key(monkeypatch):
    provision(AM)
    monkeypatch.setenv("AGENTMAIL_API_KEY", SECRET)
    inbox = provisioning.get_settings().agentmail_org_inbox
    http = FakeHttp({f"/v0/inboxes/{inbox}": 200, "/v0/api-keys": 200})
    monkeypatch.setattr(httpx, "get", http)
    assert provisioning.check_credentials()["activated"] == ["AgentMail"]
    assert [c["url"] for c in http.calls] == [f"https://api.agentmail.to/v0/inboxes/{inbox}",
                                              "https://api.agentmail.to/v0/api-keys"]
    v = vendor("AgentMail")
    assert v.status == "active"
    assert v.last_check_error == "warning: key has organization-wide access; create an inbox-scoped key"
    [mail] = emails("is active")
    assert "organization-wide" in mail.body


def test_agentmail_inbox_scoped_key_has_no_warning():
    http = FakeHttp({"/inboxes/x@agentmail.to": 200, "/api-keys": 403})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "get", http)
        mp.setattr(provisioning.get_settings(), "agentmail_org_inbox", "x@agentmail.to")
        mp.setenv("AGENTMAIL_API_KEY", SECRET)
        assert provisioning.verify({"name": "AgentMail", "category": "email",
                                    "credential_env": "AGENTMAIL_API_KEY"}) == (True, None, [])


def test_anthropic_verifier_sends_the_right_headers(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    http = FakeHttp({"/v1/models": 200})
    monkeypatch.setattr(httpx, "get", http)
    assert provisioning.verify({"name": "Anthropic", "category": "llm",
                                "credential_env": "ANTHROPIC_API_KEY"}) == (True, None, [])
    [call] = http.calls
    assert call["url"] == "https://api.anthropic.com/v1/models"
    assert call["headers"]["x-api-key"] == SECRET and call["headers"]["anthropic-version"]


def test_unknown_vendor_is_presence_only_with_a_warning(monkeypatch):
    # U3: an unknown vendor now defaults to human_confirm, so this asserts the explicit "auto" path
    provision({**REQUEST, "activation": "auto", "activation_reason": "harmless read-only token"})
    monkeypatch.setenv("ACME_ADS_TOKEN", SECRET)
    monkeypatch.setattr(httpx, "get", FakeHttp({}))  # no network call for an unknown vendor
    assert provisioning.check_credentials()["activated"] == ["Acme Ads"]
    err = vendor().last_check_error
    assert err.startswith("warning:") and "not verified" in err and "presence" in err


def test_network_error_is_a_failure_and_never_raises(monkeypatch):
    provision(LS)
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", SECRET)
    monkeypatch.setattr(httpx, "get", FakeHttp({"/v1/users/me": httpx.ConnectTimeout(f"timed out key={SECRET}")}))
    assert provisioning.check_credentials()["activated"] == []
    v = vendor("Lemon Squeezy")
    assert v.status == "provisioning" and "network error: ConnectTimeout" in v.last_check_error
    assert SECRET not in v.last_check_error and emails("does not work")


def test_the_credential_value_never_appears_anywhere(monkeypatch, caplog, capsys):
    provision(LS)
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", SECRET)
    with caplog.at_level(logging.DEBUG):
        monkeypatch.setattr(httpx, "get", FakeHttp({"/v1/users/me": httpx.ReadError(f"boom {SECRET}")}))
        provisioning.check_credentials(now=now() + timedelta(days=4))  # failure + reminder
        main(["provisioning", "list"])
        monkeypatch.setattr(httpx, "get", FakeHttp({"/v1/users/me": 200}))
        assert provisioning.check_credentials()["activated"] == ["Lemon Squeezy"]
        drain()
    assert SECRET not in caplog.text and SECRET not in capsys.readouterr().out
    with session() as s:
        stored = [str(a.detail) for a in s.scalars(select(AuditLog))]
        stored += [str(e.payload) for e in s.scalars(select(EventRow))]
        stored += [m.subject + m.body for m in s.scalars(select(OutboxEmail))]
        stored += [str(i.detail) + str(i.notes) for i in s.scalars(select(Incident))]
        stored += [str(e.__dict__) for e in s.scalars(select(ErrorRow))]
        stored += [f"{v.notes}{v.checklist}{v.last_check_error}" for v in s.scalars(select(Vendor))]
    assert len(emails("does not work")) == 1 and emails("[reminder]")
    assert not any(SECRET in x for x in stored)


# ----------------------------------------------------------------------------- U3: how a vendor becomes active
def cli(monkeypatch, capsys, *argv) -> str:
    monkeypatch.setattr("sys.argv", ["aecom", *argv])
    main()
    return capsys.readouterr().out


def waiting_unknown_vendor(monkeypatch) -> None:
    provision()
    monkeypatch.setenv("ACME_ADS_TOKEN", SECRET)
    monkeypatch.setattr(httpx, "get", FakeHttp({}))


def test_unknown_vendor_waits_for_a_human_and_asks_once(monkeypatch):
    waiting_unknown_vendor(monkeypatch)
    assert provisioning.effective_activation(vendor()) == "human_confirm"
    before = len(emails())
    assert provisioning.check_credentials()["activated"] == []
    v = vendor()
    assert v.status == "provisioning" and v.verified_at is not None
    assert any(c["step"] == provisioning.CONFIRM_STEP and not c["done"] for c in v.checklist)
    asked = emails("Confirm Acme Ads")
    assert len(asked) == 1 and len(emails()) == before + 1
    assert 'aecom provisioning confirm "Acme Ads" --by NAME' in asked[0].body
    with session() as s:
        inc = s.scalars(select(Incident).where(Incident.monitor == "provisioning_action",
                                               Incident.subject == "Acme Ads")).one()
        assert "aecom provisioning confirm" in inc.detail and inc.status != "resolved"
        assert s.scalars(select(EventRow).where(EventRow.topic == "vendor.provisioned")).all() == []
    for _ in range(3):  # the 15-minute checks stay quiet
        provisioning.check_credentials()
    assert len(emails("Confirm Acme Ads")) == 1 and vendor().status == "provisioning"


def test_confirm_cli_needs_by_then_activates_and_hands_off(monkeypatch, capsys):
    waiting_unknown_vendor(monkeypatch)
    provisioning.check_credentials()
    listed = cli(monkeypatch, capsys, "provisioning", "list")
    assert "human_confirm" in listed and "waiting_for_your_confirmation" in listed and SECRET not in listed
    with pytest.raises(SystemExit):
        cli(monkeypatch, capsys, "provisioning", "confirm", "Acme Ads")  # no --by
    assert vendor().status == "provisioning"
    out = cli(monkeypatch, capsys, "provisioning", "confirm", "Acme Ads", "--by", "kaan", "--note", "looked fine")
    assert "active" in out and SECRET not in out
    assert vendor().status == "active"
    foreman = [r for r in run_sync_runs() if r.agent_id == "foreman"]
    assert len(foreman) == 1 and "Acme Ads is provisioned" in foreman[0].task
    with session() as s:
        assert s.scalars(select(EventRow).where(EventRow.topic == "vendor.provisioned")).one().payload["vendor"] == "Acme Ads"
        inc = s.scalars(select(Incident).where(Incident.monitor == "provisioning_action")).one()
        assert inc.status == "resolved"
        actors = {a.actor for a in s.scalars(select(AuditLog).where(AuditLog.decision.in_(["confirmed", "activated"])))}
        assert "human:kaan" in actors
        assert SECRET not in " ".join(str(a.detail) for a in s.scalars(select(AuditLog)))


def run_sync_runs():
    with session() as s:
        return list(s.scalars(select(AgentRun)))


def test_confirm_refuses_without_a_working_credential(monkeypatch):
    provision()  # no credential in the environment
    monkeypatch.delenv("ACME_ADS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="not set"):
        provisioning.confirm("Acme Ads", "kaan")
    with pytest.raises(ValueError, match="--by"):
        provisioning.confirm("Acme Ads", " ")
    provision(LS)
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", SECRET)
    monkeypatch.setattr(httpx, "get", FakeHttp({"/v1/users/me": 401}))
    with pytest.raises(ValueError, match="fails"):
        provisioning.confirm("Lemon Squeezy", "kaan")
    assert vendor().status == "provisioning" and vendor("Lemon Squeezy").status == "provisioning"


def test_explicit_auto_for_an_unknown_vendor_activates_with_the_warning(monkeypatch):
    provision({**REQUEST, "activation": "auto", "activation_reason": "harmless"})
    monkeypatch.setenv("ACME_ADS_TOKEN", SECRET)
    assert provisioning.check_credentials()["activated"] == ["Acme Ads"]
    v = vendor()
    assert v.status == "active" and v.last_check_error.startswith("warning:") and v.activation_reason == "harmless"
    assert not emails("Confirm Acme Ads")


def test_known_vendor_defaults_to_auto_and_explicit_human_confirm_wins(monkeypatch):
    provision(LS)
    assert vendor("Lemon Squeezy").activation is None
    assert provisioning.effective_activation(vendor("Lemon Squeezy")) == "auto"
    monkeypatch.setitem(provisioning.VERIFIERS, "lemonsqueezy", lambda v, c: (True, None, []))
    monkeypatch.setenv("LEMONSQUEEZY_API_KEY", SECRET)
    assert provisioning.check_credentials()["activated"] == ["Lemon Squeezy"]
    assert provisioning.effective_activation({"name": "Lemon Squeezy", "category": "platform",
                                              "activation": "human_confirm"}) == "human_confirm"


def test_invalid_activation_is_rejected_and_values_show_in_the_approval():
    out = run_sync(gate_builtin("request_provisioning", "ceo").ainvoke({**REQUEST, "activation": "yolo"}))
    assert out.startswith("ESCALATED"), out  # the gate parks it; the tool refuses when executed
    approval_id = out.split("id=")[1].split(")")[0]
    approvals.approve(approval_id, "tester")  # the tool refuses on execution: the approval fails, nothing is created
    with session() as s:
        failed = s.get(Approval, approval_id)
        assert failed.status == "failed" and "activation must be" in failed.result
    assert not [v for v in all_vendors() if v.name == "Acme Ads"]
    out = run_sync(gate_builtin("request_provisioning", "ceo").ainvoke(
        {**REQUEST, "activation": "human_confirm", "activation_reason": "unknown vendor"}))
    with session() as s:
        a = s.get(Approval, out.split("id=")[1].split(")")[0])
        assert a.args["activation"] == "human_confirm" and a.args["activation_reason"] == "unknown vendor"
    approvals.approve(a.id, "tester")
    v = vendor()
    assert (v.activation, v.activation_reason) == ("human_confirm", "unknown vendor")


def all_vendors():
    with session() as s:
        return list(s.scalars(select(Vendor)))
