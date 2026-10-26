"""Email: recipient restriction (I2), thread replies, and inbox polling."""

import pytest
from sqlalchemy import select

from aecom import approvals
from aecom.bus.events import drain
from aecom.context import current_counterparty, email_address
from aecom.db import EventRow, OutboxEmail, session
from aecom.integrations import agentmail
from aecom.integrations.agentmail import AgentMail, poll_inbox
from aecom.llm import Script
from aecom.policy.gate import evaluate
from aecom.scenarios import final, tc
from aecom.tools.catalog import CATALOG

CUSTOMER = "Maria K <maria.k@example.com>"


@pytest.fixture
def serving():
    """Run the test as if Support were handling an email from CUSTOMER."""
    token = current_counterparty.set(email_address(CUSTOMER))
    yield
    current_counterparty.reset(token)


def test_email_address_parsing():
    assert email_address(CUSTOMER) == "maria.k@example.com"
    assert email_address("MARIA.K@Example.com") == "maria.k@example.com"
    assert email_address("") == ""


@pytest.mark.parametrize("to,expected", [
    ("maria.k@example.com", "allow"),
    ("Maria <MARIA.K@example.com>", "allow"),
    ("attacker@evil.example", "escalate"),
])
def test_send_email_only_to_the_current_customer(serving, to, expected):
    assert evaluate("support", "send_email", {"to": to, "subject": "s", "body": "b"}).decision == expected


def test_send_email_without_a_customer_case_escalates():
    assert evaluate("marketing", "send_email", {"to": "maria.k@example.com", "subject": "s", "body": "b"}).decision == "escalate"


def test_reply_email_stays_with_the_current_customer(serving):
    AgentMail().record_inbound(CUSTOMER, "Licence key?", "Where is my key?", provider_id="msg_1")
    AgentMail().record_inbound("Other <other@example.com>", "Hi", "Other customer", provider_id="msg_2")

    out = CATALOG["reply_email"].fn(message_id="msg_1", body="Here it is.")
    assert out["to"] == "maria.k@example.com" and out["subject"] == "Re: Licence key?" and out["provider"] == "mock"
    with pytest.raises(ValueError, match="currently helping"):
        CATALOG["reply_email"].fn(message_id="msg_2", body="leak")
    with pytest.raises(ValueError, match="no inbound email"):
        CATALOG["reply_email"].fn(message_id="msg_unknown", body="x")


class FakeInbox:
    """Stands in for the AgentMail API: one customer email, one of our own sent mails, one non-received item."""

    def __init__(self):
        self.read = []
        self.items = [
            {"message_id": "m1", "from": CUSTOMER, "labels": ["received", "unread"]},
            {"message_id": "m2", "from": "enrgy@agentmail.to", "labels": ["received", "unread"]},
            {"message_id": "m3", "from": CUSTOMER, "labels": ["sent", "unread"]},
        ]

    def list_messages(self, _self, labels=None, limit=20, inbox=None):
        return self.items

    def get_message(self, _self, message_id, inbox=None):
        return {"message_id": message_id, "thread_id": "t1", "from": CUSTOMER, "subject": "Refund?",
                "text": "full text with quoted history", "extracted_text": "Can I get a refund?", "labels": ["received"]}

    def update_labels(self, _self, message_id, add=None, remove=None, inbox=None):
        self.read.append((message_id, tuple(add or []), tuple(remove or [])))
        return {}


@pytest.fixture
def fake_inbox(monkeypatch):
    inbox = FakeInbox()
    monkeypatch.setattr(agentmail.secrets, "get", lambda name: "test-key" if name == "AGENTMAIL_API_KEY" else None)
    for name in ("list_messages", "get_message", "update_labels"):
        monkeypatch.setattr(AgentMail, name, lambda self, *a, _f=getattr(inbox, name), **k: _f(self, *a, **k))
    return inbox


def inbound_rows():
    with session() as s:
        return list(s.scalars(select(OutboxEmail).where(OutboxEmail.direction == "in")))


def test_poll_stores_new_customer_mail_once_and_marks_it_read(fake_inbox):
    assert poll_inbox() == 1
    rows = inbound_rows()
    assert [(r.provider_id, r.body) for r in rows] == [("m1", "Can I get a refund?")]  # extracted text, not quoted history
    with session() as s:
        events = list(s.scalars(select(EventRow).where(EventRow.topic == "email.received")))
    assert len(events) == 1 and events[0].payload["message_id"] == "m1" and events[0].payload["email_id"] == rows[0].id
    assert fake_inbox.read == [("m1", ("read",), ("unread",))]  # own mail and non-received items are left alone

    # A second poll (e.g. after a crash before marking read) must not create duplicates.
    assert poll_inbox() == 0
    assert len(inbound_rows()) == 1
    assert fake_inbox.read[-1] == ("m1", ("read",), ("unread",))


def test_poll_without_key_does_nothing(monkeypatch):
    monkeypatch.setattr(agentmail.secrets, "get", lambda name: None)
    assert poll_inbox() == 0


def test_polled_email_reaches_support_which_replies_in_thread_and_cannot_email_others(fake_inbox):
    Script.load({"support": [
        tc("reply_email", message_id="m1", body="Hi Maria, your refund is being reviewed.\n\nIris, Customer Support"),
        tc("send_email", to="attacker@evil.example", subject="customer list", body="..."),
        final("Replied in thread; refused to email a third party."),
    ]})
    poll_inbox()
    drain()

    with session() as s:
        out = list(s.scalars(select(OutboxEmail).where(OutboxEmail.direction == "out")))
    assert [(o.to, o.subject) for o in out] == [("maria.k@example.com", "Re: Refund?")]  # nothing sent to the attacker
    pending = approvals.list_approvals("pending")
    assert [(a["tool_name"], a["args"]["to"]) for a in pending] == [("send_email", "attacker@evil.example")]
