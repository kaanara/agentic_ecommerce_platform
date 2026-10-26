"""J3/J4: immutable report snapshots. Code computes the numbers (data), agents only write the commentary (text).

A report is never edited: a correction is a new row with the next version for the same kind and period.
The number check rejects commentary that quotes a figure which isn't in the report's data.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select

from aecom import metrics
from aecom.db import Report, session

_KIND = re.compile(r"[a-z][a-z0-9_]{0,39}")
# Stripped before extraction: ISO dates/timestamps and list markers at the start of a line ("1." / "2)").
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\b")
_LIST_MARKER = re.compile(r"^(\s*)\d{1,3}[.)](?=\s)", re.MULTILINE)
# A number plus whatever is glued to it. Scale suffixes (k, m, bn, "million") multiply the value; any other letters
# ("37x", "250EUR", "999orders") still leave the number checked. Never skip a number because letters follow it.
_NUMBER = re.compile(
    r"(?<![\w.,])(?P<sign>[-−])?(?P<cur>(?:[€$£]|(?i:eur|usd|gbp)(?![a-z]))\s?)?(?P<num>\d+(?:[.,]\d+)*)"
    r"(?P<scale>(?i:k|m|mn|b|bn)(?![A-Za-z])|\s?(?i:thousand|million|billion)\b)?"
    r"(?P<pct>\s?%|\s?(?i:pct|percent)\b)?(?P<suffix>[A-Za-z]+)?"
)
_SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_COUNT_WORD = re.compile(r"[\s-]+[A-Za-z]")


def build_data(kind: str, period_name: str, today: date | datetime | None = None) -> dict[str, Any]:
    """The report's numbers: P&L and KPIs from aecom.metrics for a named period."""
    start, end = metrics.period(period_name, today)
    return {
        "kind": kind,
        "period": {"name": period_name, "start": start.isoformat(), "end": end.isoformat(), "days": (end - start).days},
        "pnl": metrics.pnl(start, end),
        "kpis": metrics.kpis(start, end),
    }


def _candidates(num: str) -> list[tuple[float, int]]:
    """(value, decimals written) readings of '1,234.56', '1.234,56', '12,5', '40.8'. Ambiguous '1.234' gives both."""
    if "." in num and "," in num:
        dec = "." if num.rfind(".") > num.rfind(",") else ","
        whole, frac = num.rsplit(dec, 1)
        return [(float(re.sub(r"[.,]", "", whole) + "." + frac), len(frac))]
    sep = "." if "." in num else "," if "," in num else None
    if sep is None:
        return [(float(num), 0)]
    parts = num.split(sep)
    if len(parts) > 2:  # 1,234,567 / 1.234.567
        return [(float("".join(parts)), 0)]
    whole, frac = parts
    decimal = (float(f"{whole}.{frac}"), len(frac))
    return [decimal, (float(whole + frac), 0)] if len(frac) == 3 else [decimal]


def _flatten(data: Any) -> list[float]:
    if isinstance(data, bool) or data is None:
        return []
    if isinstance(data, (int, float)):
        return [float(data)]
    if isinstance(data, dict):
        return [v for x in data.values() for v in _flatten(x)]
    if isinstance(data, (list, tuple)):
        return [v for x in data for v in _flatten(x)]
    return []


def _matches(value: float, decimals: int, known: list[float], scale: float = 1.0) -> bool:
    tolerance = (0.5 * 10 ** -decimals + 1e-9) * scale  # "40.8" matches 40.83, "71%" matches 71.4, "€1.2k" 1180
    return any(abs(abs(k) - value * scale) <= tolerance for k in known)


def number_check(text: str, data: dict[str, Any]) -> list[str]:
    """Numbers quoted in text that don't appear in data (after rounding to the precision written)."""
    cleaned = _LIST_MARKER.sub(r"\1", _DATE.sub(" ", text))
    known = _flatten(data)
    unknown: list[str] = []
    for m in _NUMBER.finditer(cleaned):
        num = m["num"]
        plain = not (m["cur"] or m["pct"] or m["sign"] or m["scale"] or m["suffix"])
        scale = _SCALES[m["scale"].strip().lower()] if m["scale"] else 1.0
        if plain and num.isdigit():
            value = int(num)
            if len(num) == 4 and 1900 <= value <= 2100:
                continue  # a year
            if value <= 10 and _COUNT_WORD.match(cleaned, m.end()):
                continue  # "3 insights", "2 campaigns"
        if not any(_matches(v, d, known, scale) for v, d in _candidates(num)):
            unknown.append(m.group(0).strip())
    return unknown


def _sha(data: dict[str, Any], text: str) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((canonical + text).encode()).hexdigest()


def _to_dict(r: Report) -> dict[str, Any]:
    return {"id": r.id, "kind": r.kind, "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None, "version": r.version, "data": r.data,
            "text": r.text, "sha256": r.sha256, "created_by": r.created_by, "created_at": r.created_at.isoformat()}


def create_report(kind: str, period_name: str, text: str, created_by: str = "system",
                  today: date | datetime | None = None) -> dict[str, Any]:
    """Snapshot a new report version. Raises ValueError if the text quotes numbers that aren't in the data."""
    if not _KIND.fullmatch(kind or ""):
        raise ValueError(f"invalid report kind {kind!r}; use lowercase snake_case, e.g. daily_flash or weekly_review")
    if not (text or "").strip():
        raise ValueError("report text is empty")
    data = build_data(kind, period_name, today)
    if unknown := number_check(text, data):
        raise ValueError(f"report mentions numbers not in its data: {', '.join(unknown)}. "
                         "Quote only figures from get_pnl/get_kpis for this period.")
    start, end = metrics.period(period_name, today)
    with session() as s:
        previous = s.scalar(select(func.max(Report.version))
                            .where(Report.kind == kind, Report.period_start == start, Report.period_end == end)) or 0
        r = Report(kind=kind, period_start=start, period_end=end, version=previous + 1, data=data, text=text,
                   sha256=_sha(data, text), created_by=created_by)
        s.add(r)
        s.flush()
        return _to_dict(r)


def get_report(report_id: str) -> dict[str, Any] | None:
    with session() as s:
        r = s.get(Report, report_id)
        return _to_dict(r) if r else None


def list_reports(kind: str | None = None) -> list[dict[str, Any]]:
    """Newest first."""
    with session() as s:
        q = select(Report).order_by(Report.created_at.desc(), Report.version.desc())
        if kind is not None:
            q = q.where(Report.kind == kind)
        return [_to_dict(r) for r in s.scalars(q)]


def latest(kind: str) -> dict[str, Any] | None:
    reports = list_reports(kind)
    return reports[0] if reports else None
