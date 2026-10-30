"""Owner notifications: one router for everything that should reach the owner (STUB: interface fixed in W0, built in W2).

``route(event)`` delivers an event on the best available channel: a paired Telegram chat first, then ntfy, otherwise it
only appears in the dashboard inbox. Event keys: ``kind`` (``message`` | ``approval`` | ``incident`` | ``diagnosis`` |
``flash`` | ``settings``), ``text``, ``urgency`` (``info`` | ``normal`` | ``high``; high ignores quiet hours),
optional ``conversation_id``, ``approval_id``, ``agent_id``. Returns ``{channel_name: delivered}``.
"""

from __future__ import annotations

from typing import Any


def route(event: dict[str, Any]) -> dict[str, bool]:
    raise NotImplementedError("notify.route is built in wave 2 (builder F)")
