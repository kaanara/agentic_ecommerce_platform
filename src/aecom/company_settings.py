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


def agent_enabled(agent_id: str) -> bool:
    """Per-agent switch (setting ``agent_enabled:<id>``), default on. Off = the agent's LLM runs are skipped.

    Reflexes (handlers.invoke_as → gate, e.g. delivery and restock) don't run the agent and are not affected.
    """
    return get_setting(f"agent_enabled:{agent_id}", "on") != "off"


def set_agent_enabled(agent_id: str, on: bool) -> None:
    set_setting(f"agent_enabled:{agent_id}", "on" if on else "off")
