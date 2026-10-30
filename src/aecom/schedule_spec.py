"""Schedule strings that the owner edits in the dashboard, turned into APScheduler trigger arguments.

Grammar: ``30s`` / ``15m`` / ``6h`` (an interval) or ``cron:MIN HOUR DAY MONTH WEEKDAY`` (five fields, as in crontab,
each field a number, ``*`` or a comma list). Anything else is rejected, so a typo in the UI can never stop a job.
"""

from __future__ import annotations

import re
from typing import Any

_INTERVAL = re.compile(r"^(\d+)\s*([smh])$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours"}
_FIELDS = ("minute", "hour", "day", "month", "day_of_week")
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
MIN_INTERVAL_S = 5


_WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekday_names(part: str) -> str:
    """Cron numbers weekdays 0/7=Sunday..6=Saturday but APScheduler numbers 0=Monday, so use names."""
    return part if part == "*" else ",".join(_WEEKDAYS[int(i)] for i in part.split(","))


def parse_schedule(spec: str) -> tuple[str, dict[str, Any]]:
    """('interval', {'minutes': 15}) or ('cron', {'minute': '15', 'hour': '3', ...}). Raises ValueError."""
    spec = (spec or "").strip().lower()
    if m := _INTERVAL.match(spec):
        n, unit = int(m.group(1)), m.group(2)
        if n <= 0:
            raise ValueError("interval must be positive")
        if n * {"s": 1, "m": 60, "h": 3600}[unit] < MIN_INTERVAL_S:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_S}s")
        return "interval", {_UNITS[unit]: n}
    if spec.startswith("cron:"):
        parts = spec[5:].split()
        if len(parts) != 5:
            raise ValueError("cron needs 5 fields: minute hour day month weekday")
        out: dict[str, Any] = {}
        for name, (lo, hi), part in zip(_FIELDS, _RANGES, parts, strict=True):
            if part != "*":
                for item in part.split(","):
                    if not item.isdigit() or not lo <= int(item) <= hi:
                        raise ValueError(f"{name}: '{item}' must be * or a number {lo}-{hi}")
            out[name] = _weekday_names(part) if name == "day_of_week" else part
        return "cron", out
    raise ValueError("use 30s, 15m, 6h or cron:M H D M W")


def is_valid(spec: str) -> bool:
    try:
        parse_schedule(spec)
        return True
    except ValueError:
        return False


def add_job(sched: Any, func: Any, spec: str, job_id: str, fallback: str) -> None:
    """Add (or replace) a job from a schedule string; an invalid string falls back to the built-in default."""
    try:
        kind, kwargs = parse_schedule(spec)
    except ValueError:
        kind, kwargs = parse_schedule(fallback)
    sched.add_job(func, kind, id=job_id, replace_existing=True, **kwargs)
