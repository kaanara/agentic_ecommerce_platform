"""Channel interface (fixed in W0; Telegram is built in W2 builder E, WhatsApp in W4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Button:
    label: str
    data: str  # e.g. "approve:appr_123"


@dataclass
class Inbound:
    channel: str
    external_chat_id: str
    text: str
    message_id: str = ""
    attachments: list[dict] = field(default_factory=list)
    # Set instead of `text` when the update is a button tap (Telegram callback_query); e.g. "approve:appr_123".
    callback_data: str = ""
    callback_id: str = ""  # provider id of the callback, used to acknowledge it (e.g. Telegram's answerCallbackQuery)


@dataclass
class Reply:
    """One message to send back to the chat that produced an `Inbound`."""

    text: str
    buttons: list[Button] | None = None


class Channel(Protocol):
    name: str

    def enabled(self) -> bool: ...

    def send(self, external_chat_id: str, text: str, buttons: list[Button] | None = None) -> bool: ...

    def parse_inbound(self, update: dict) -> Inbound | None: ...
