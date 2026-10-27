"""Marketing consent (I14): opt-in only newsletters, STOP replies, bounce/complaint suppression. Offline."""

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aecom import consent
from aecom.api import app as api_app
from aecom.api import webhooks
from aecom.bus.events import drain, publish
from aecom.db import AuditLog, Customer, OutboxEmail, Suppression, session
from aecom.graph import agents
from aecom.tools.catalog import CATALOG

AM_SECRET = "whsec_" + base64.b64encode(b"agentmail-test-key-0123456789").decode()


def add_customer(email: str, channel: str | None = None) -> str:
    with session() as s:
        c = Customer(email=email, acquisition_channel=channel)
        s.add(c)
        s.flush()
        return c.id


def outbox(direction: str = "out") -> list[OutboxEmail]:
    with session() as s:
        return list(s.scalars(select(OutboxEmail).where(OutboxEmail.direction == direction).order_by(OutboxEmail.id)))


def suppression(email: str) -> str | None:
    with session() as s:
        row = s.get(Suppression, email)
        return row.reason if row else None


def newsletter(segment: str = "all") -> dict:
    return CATALOG["send_newsletter"].fn(subject="News", body="Our new course is out.", segment=segment)


# --------------------------------------------------------------------------- newsletter
def test_newsletter_reaches_nobody_by_default():
    with session() as s:
        customers = s.scalar(select(func.count()).select_from(Customer)) or 0
    add_customer("new@example.com")
    out = newsletter()
    assert out == {"sent": 0, "skipped_no_consent": customers + 1, "segment": "all"}
    assert outbox() == []


def test_newsletter_reaches_only_consenting_customers():
    add_customer("Yes@Example.com", channel="google_ads")
    add_customer("no@example.com", channel="google_ads")
    add_customer("gone@example.com", channel="google_ads")
    add_customer("bounced@example.com", channel="google_ads")
    consent.record_opt_in("yes@example.com", "checkout")
    consent.record_opt_in("gone@example.com", "form")
    consent.unsubscribe("gone@example.com")
    consent.record_opt_in("bounced@example.com", "form")
    consent.suppress("bounced@example.com", "bounce")

    out = newsletter("google_ads")
    assert out == {"sent": 1, "skipped_no_consent": 3, "segment": "google_ads"}
    [mail] = outbox()
    assert mail.to == "Yes@Example.com"
    assert mail.body.startswith("Our new course is out.") and mail.body.endswith("Reply STOP to unsubscribe.")
    assert consent.is_marketable("YES@example.com") and not consent.is_marketable("no@example.com")


# --------------------------------------------------------------------------- opt-in / unsubscribe
def test_opt_in_after_unsubscribe_works_but_after_bounce_or_complaint_is_refused():
    cid = add_customer("maria@example.com")
    consent.record_opt_in("maria@example.com", "form")
    first = consent.unsubscribe("Maria <MARIA@example.com>")
    assert first["changed"] and suppression("maria@example.com") == "unsubscribe"
    assert consent.unsubscribe("maria@example.com")["changed"] is False  # idempotent
    with session() as s:
        c = s.get(Customer, cid)
        assert c.marketing_consent is False and c.unsubscribed_at is not None

    assert consent.record_opt_in("maria@example.com", "form")["marketing_consent"] is True
    assert suppression("maria@example.com") is None and consent.is_marketable("maria@example.com")
    with session() as s:
        c = s.get(Customer, cid)
        assert c.unsubscribed_at is None and c.marketing_consent_source == "form" and c.marketing_consent_at

    for addr, reason in (("maria@example.com", "bounce"), ("other@example.com", "complaint")):
        consent.suppress(addr, reason)
        with pytest.raises(ValueError, match=reason):
            consent.record_opt_in(addr, "form")
        assert not consent.is_marketable(addr)
    consent.unsubscribe("maria@example.com")  # an unsubscribe never downgrades a bounce
    assert suppression("maria@example.com") == "bounce"
    with pytest.raises(ValueError, match="unknown suppression reason"):
        consent.suppress("x@example.com", "because")


def test_audit_holds_the_customer_id_never_the_address():
    cid = add_customer("Private.Person@example.com")
    consent.record_opt_in("private.person@example.com", "checkout")
    consent.unsubscribe("private.person@example.com")
    consent.suppress("private.person@example.com", "complaint")
    consent.suppress("stranger@example.com", "bounce")
    with session() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action.like("marketing_%")).order_by(AuditLog.id)))
    assert [r.action for r in rows] == ["marketing_opt_in", "marketing_unsubscribe", "marketing_suppressed",
                                        "marketing_suppressed"]
    assert [r.detail["customer_id"] for r in rows] == [cid, cid, cid, None]
    dump = json.dumps([r.detail for r in rows]).lower()
    assert "private.person" not in dump and "stranger" not in dump and "@" not in dump


# --------------------------------------------------------------------------- STOP replies
@pytest.fixture
def no_support(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("Support must not run for an unsubscribe")

    monkeypatch.setattr(agents, "run_agent", fail)


@pytest.mark.parametrize("subject,body", [
    ("Re: News", "STOP\n\n> Our new course is out."),
    ("unsubscribe", ""),
    ("Re: Newsletter", "  Abmelden!\nDanke"),
])
def test_stop_reply_unsubscribes_confirms_and_skips_support(no_support, subject, body):
    add_customer("maria@example.com")
    consent.record_opt_in("maria@example.com", "checkout")
    publish("email.received", {"from": "Maria <Maria@Example.com>", "subject": subject, "body": body,
                               "message_id": "m-stop"})
    drain()
    assert not consent.is_marketable("maria@example.com") and suppression("maria@example.com") == "unsubscribe"
    [inbound] = outbox("in")
    assert inbound.provider_id == "m-stop"
    [reply] = outbox()
    assert reply.to == "maria@example.com" and reply.subject.startswith("Re:") and "unsubscribed" in reply.body


def test_stop_without_message_id_is_confirmed_by_new_email(no_support):
    publish("email.received", {"from": "stranger@example.com", "subject": "STOP", "body": ""})
    drain()
    assert suppression("stranger@example.com") == "unsubscribe"
    [mail] = outbox()
    assert mail.to == "stranger@example.com" and mail.subject == "Re: STOP"


def test_ordinary_email_mentioning_stop_still_goes_to_support(monkeypatch):
    seen = []

    async def support(agent_id, task, **kwargs):
        seen.append(agent_id)
        return agents.AgentResult(agent_id=agent_id, output="done")

    monkeypatch.setattr(agents, "run_agent", support)
    publish("email.received", {"from": "maria@example.com", "subject": "Download won't stop failing",
                               "body": "Please stop the error, it stops at 50%.", "message_id": "m-help"})
    drain()
    assert seen == ["support"] and suppression("maria@example.com") is None


# --------------------------------------------------------------------------- bounce / complaint webhooks
def svix_headers(raw: bytes, msg_id: str) -> dict[str, str]:
    ts = int(time.time())
    key = base64.b64decode(AM_SECRET.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}",
            "content-type": "application/json"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(webhooks.secrets, "get", lambda name: AM_SECRET if name == "AGENTMAIL_WEBHOOK_SECRET" else None)
    return TestClient(api_app.app)


def test_bounce_and_complaint_webhooks_suppress_recipients(client):
    add_customer("bounce@example.com")
    consent.record_opt_in("bounce@example.com", "checkout")
    bounce = json.dumps({"type": "event", "event_type": "message.bounced", "event_id": "evt_b", "bounce": {
        "inbox_id": "shop@agentmail.to", "message_id": "m1", "type": "Permanent", "sub_type": "General",
        "recipients": [{"address": "Bounce@Example.com", "status": "bounced"}]}}).encode()
    complaint = json.dumps({"type": "event", "event_type": "message.complained", "event_id": "evt_c", "complaint": {
        "inbox_id": "shop@agentmail.to", "message_id": "m2", "type": "abuse", "sub_type": "spam",
        "recipients": ["angry@example.com"]}}).encode()

    r = client.post("/webhooks/agentmail", content=bounce, headers=svix_headers(bounce, "msg_b"))
    assert r.status_code == 200 and r.json()["status"] == "suppressed" and r.json()["suppressed"] == 1
    r = client.post("/webhooks/agentmail", content=complaint, headers=svix_headers(complaint, "msg_c"))
    assert r.status_code == 200 and r.json()["suppressed"] == 1
    assert suppression("bounce@example.com") == "bounce" and suppression("angry@example.com") == "complaint"
    assert not consent.is_marketable("bounce@example.com")

    again = client.post("/webhooks/agentmail", content=bounce, headers=svix_headers(bounce, "msg_b"))
    assert again.json()["duplicate"] is True
    bad = {**svix_headers(complaint, "msg_x"), "svix-signature": "v1,AAAA"}
    other = complaint.replace(b"angry@", b"forged@")
    assert client.post("/webhooks/agentmail", content=other, headers=bad).status_code == 401
    assert suppression("forged@example.com") is None
