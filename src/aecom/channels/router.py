"""Inbound messages -> paired-owner check -> command or chat turn (STUB: built in W2 builder E).

Unpaired chats are ignored and audited. Commands: /pair CODE, /agent ID, /new, /status, /approvals, /stop.
Returns the reply texts to send back.
"""

from __future__ import annotations

from aecom.channels.base import Inbound


def handle_inbound(msg: Inbound) -> list[str]:
    raise NotImplementedError("channels.router.handle_inbound is built in wave 2 (builder E)")
