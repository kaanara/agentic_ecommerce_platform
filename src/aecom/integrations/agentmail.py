"""AgentMail (https://docs.agentmail.to) — email for agents.

Every send is recorded in ``ops_outbox_emails``. Real delivery happens only when
``AGENTMAIL_LIVE=true`` *and* a key is available; otherwise messages stay in the local outbox
(the default, because seed customers are fake addresses).
"""

from __future__ import annotations

from typing import Any

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

    def record_inbound(self, sender: str, subject: str, text: str, inbox: str | None = None) -> str:
        inbox = inbox or self.settings.agentmail_org_inbox
        with session() as s:
            row = OutboxEmail(direction="in", inbox=inbox, to=inbox, sender=sender, subject=subject, body=text,
                              provider="agentmail" if self.live else "mock")
            s.add(row)
            s.flush()
            return row.id

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
