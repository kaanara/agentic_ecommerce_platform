"""AgentMail (https://docs.agentmail.to) — email for agents.

Every send is recorded in ``ops_outbox_emails``. Real delivery happens only when
``AGENTMAIL_LIVE=true`` *and* a key is available; otherwise messages stay in the local outbox
(the default, because seed customers are fake addresses).

Inbound mail arrives by webhook (``POST /webhooks/agentmail``) or by ``poll_inbox()``; reading needs only a key.
The app should use an inbox-scoped key (``scripts/agentmail_inbox_key.py``), not an organization key.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from aecom.config import get_settings
from aecom.db import OutboxEmail, session
from aecom.secrets import secrets


class AgentMail:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.key = secrets.get("AGENTMAIL_API_KEY")

    @property
    def live(self) -> bool:
        return bool(self.settings.agentmail_live and self.key)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.settings.agentmail_base_url, timeout=20,
                            headers={"Authorization": f"Bearer {self.key}"})

    def send(self, to: str, subject: str, text: str, inbox: str | None = None) -> dict[str, Any]:
        inbox = inbox or self.settings.agentmail_org_inbox
        provider_id = None
        if self.live:
            with self._client() as c:
                r = c.post(f"/inboxes/{inbox}/messages/send", json={"to": [to], "subject": subject, "text": text})
                r.raise_for_status()
                provider_id = r.json().get("message_id")
        with session() as s:
            row = OutboxEmail(direction="out", inbox=inbox, to=to, sender=inbox, subject=subject, body=text,
                              provider="agentmail" if self.live else "mock", provider_id=provider_id)
            s.add(row)
            s.flush()
            return {"email_id": row.id, "provider": row.provider, "to": to, "subject": subject}

    def reply(self, message_id: str, text: str, to: str, subject: str, inbox: str | None = None) -> dict[str, Any]:
        """Reply inside the thread of an inbound message. Recorded in the outbox like `send`."""
        inbox = inbox or self.settings.agentmail_org_inbox
        provider_id = None
        if self.live:
            with self._client() as c:
                r = c.post(f"/inboxes/{inbox}/messages/{quote(message_id, safe='')}/reply", json={"text": text})
                r.raise_for_status()
                provider_id = r.json().get("message_id")
        with session() as s:
            row = OutboxEmail(direction="out", inbox=inbox, to=to, sender=inbox, subject=subject, body=text,
                              provider="agentmail" if self.live else "mock", provider_id=provider_id)
            s.add(row)
            s.flush()
            return {"email_id": row.id, "provider": row.provider, "to": to, "subject": subject, "in_reply_to": message_id}

    def record_inbound(self, sender: str, subject: str, text: str, inbox: str | None = None,
                       provider_id: str | None = None) -> str:
        inbox = inbox or self.settings.agentmail_org_inbox
        with session() as s:
            if provider_id:  # idempotent: event retries must not store the same email twice
                from sqlalchemy import select

                existing = s.scalars(select(OutboxEmail.id).where(OutboxEmail.direction == "in",
                                                                 OutboxEmail.provider_id == provider_id)).first()
                if existing:
                    return existing
            row = OutboxEmail(direction="in", inbox=inbox, to=inbox, sender=sender, subject=subject, body=text,
                              provider="agentmail" if provider_id else "mock", provider_id=provider_id)
            s.add(row)
            s.flush()
            return row.id

    # Reading needs only a key: AGENTMAIL_LIVE governs sending, not reading.
    def list_messages(self, labels: list[str] | None = None, limit: int = 20, inbox: str | None = None) -> list[dict[str, Any]]:
        inbox = inbox or self.settings.agentmail_org_inbox
        params: dict[str, Any] = {"limit": limit, "ascending": True}
        if labels:
            params["labels"] = labels
        with self._client() as c:
            return c.get(f"/inboxes/{inbox}/messages", params=params).raise_for_status().json().get("messages", [])

    def get_message(self, message_id: str, inbox: str | None = None) -> dict[str, Any]:
        inbox = inbox or self.settings.agentmail_org_inbox
        with self._client() as c:
            return c.get(f"/inboxes/{inbox}/messages/{quote(message_id, safe='')}").raise_for_status().json()

    def update_labels(self, message_id: str, add: list[str] | None = None, remove: list[str] | None = None,
                      inbox: str | None = None) -> dict[str, Any]:
        inbox = inbox or self.settings.agentmail_org_inbox
        with self._client() as c:
            return c.patch(f"/inboxes/{inbox}/messages/{quote(message_id, safe='')}",
                           json={"add_labels": add or [], "remove_labels": remove or []}).raise_for_status().json()

    # Admin helpers (used once by a human during setup; not exposed as agent tools)
    def list_inboxes(self) -> Any:
        with self._client() as c:
            return c.get("/inboxes").raise_for_status().json()

    def list_api_keys(self) -> Any:
        with self._client() as c:
            return c.get("/api-keys").raise_for_status().json()

    def create_api_key(self, name: str) -> Any:
        with self._client() as c:
            return c.post("/api-keys", json={"name": name}).raise_for_status().json()


def poll_inbox(limit: int = 20) -> int:
    """Pull unread inbound mail and hand each new message to Support as an `email.received` event.

    Works without a public webhook URL and is the backup for the webhook. The local email row and its event
    are written in one commit; the message is marked read only afterwards, so a crash in between means the
    next poll sees it again and, finding it already stored, just marks it read (at-least-once, no duplicates).
    """
    from sqlalchemy import select

    from aecom.context import email_address
    from aecom.db import EventRow

    mail = AgentMail()
    if not mail.key:
        return 0
    own = email_address(mail.settings.agentmail_org_inbox)
    new = 0
    for item in mail.list_messages(labels=["unread"], limit=limit):
        mid = item["message_id"]
        if "received" not in item.get("labels", []) or email_address(item.get("from", "")) == own:
            continue
        with session() as s:
            seen = s.scalars(select(OutboxEmail.id).where(OutboxEmail.direction == "in", OutboxEmail.provider_id == mid)).first()
        if seen is None:
            m = mail.get_message(mid)
            body = m.get("extracted_text") or m.get("text") or m.get("preview") or ""
            with session() as s:
                row = OutboxEmail(direction="in", inbox=mail.settings.agentmail_org_inbox, to=mail.settings.agentmail_org_inbox,
                                  sender=m.get("from", ""), subject=m.get("subject") or "", body=body,
                                  provider="agentmail", provider_id=mid)
                s.add(row)
                s.flush()
                s.add(EventRow(topic="email.received", source="agentmail", payload={
                    "from": m.get("from", ""), "subject": m.get("subject") or "", "body": body, "message_id": mid,
                    "thread_id": m.get("thread_id"), "email_id": row.id, "labels": m.get("labels", [])}))
            new += 1
        mail.update_labels(mid, add=["read"], remove=["unread"])
    return new
