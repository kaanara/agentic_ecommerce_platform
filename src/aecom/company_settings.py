"""Runtime-tunable company settings (kill switch, agent pause, budgets) stored in ops_settings."""

from __future__ import annotations

from aecom.db import Setting, session


def get_setting(key: str, default: str | None = None) -> str | None:
    with session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with session() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))


def kill_switch_on() -> bool:
    return get_setting("kill_switch", "off") == "on"


def agents_paused_on() -> bool:
    """Softer than the kill switch: agents stop, but customer deliveries keep running."""
    return get_setting("agents_paused", "off") == "on"
