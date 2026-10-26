"""Runtime-tunable company settings (kill switch, agent pause, safe mode, budgets) stored in ops_settings."""

from __future__ import annotations

from aecom.audit import audit
from aecom.db import Setting, now, session


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


def safe_mode_on() -> bool:
    """Between pause and kill switch: only deliveries and customer replies run, nothing that spends money.

    Switched on by the monitors when a P1 stays unacknowledged; only a human switches it off."""
    return get_setting("safe_mode", "off") == "on"


def enter_safe_mode(reason: str) -> bool:
    """Turn safe mode on (setting safe_mode + safe_mode_since + safe_mode_reason). Returns False if it already was."""
    if safe_mode_on():
        return False
    set_setting("safe_mode", "on")
    set_setting("safe_mode_since", now().isoformat())
    set_setting("safe_mode_reason", reason)
    audit("system", "safe_mode", "on", {"reason": reason})
    return True


def leave_safe_mode(by: str) -> bool:
    """A human turns safe mode off. Returns False if it was not on."""
    if not by.strip():
        raise ValueError("leaving safe mode needs the name of the human (by)")
    if not safe_mode_on():
        return False
    reason = get_setting("safe_mode_reason", "")
    set_setting("safe_mode", "off")
    set_setting("safe_mode_since", "")
    set_setting("safe_mode_reason", "")
    audit(f"human:{by}", "safe_mode", "off", {"was_reason": reason})
    return True


def agent_enabled(agent_id: str) -> bool:
    """Per-agent switch (setting ``agent_enabled:<id>``), default on. Off = the agent's LLM runs are skipped.

    Reflexes (handlers.invoke_as → gate, e.g. delivery and restock) don't run the agent and are not affected.
    """
    return get_setting(f"agent_enabled:{agent_id}", "on") != "off"


def set_agent_enabled(agent_id: str, on: bool) -> None:
    set_setting(f"agent_enabled:{agent_id}", "on" if on else "off")
