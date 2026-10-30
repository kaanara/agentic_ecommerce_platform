"""Period close (J8) and ledger immutability verification (J11).

An accounting month (``YYYY-MM``) closes only once it is entirely in the past, and only in order: every earlier
month that has ledger lines must already be closed. Closing stores a sha256 fingerprint of that month's ledger
lines (``ledger_fingerprint``) plus how many there are, so a later raw-SQL edit or a backdated insert is caught by
``verify`` even though it bypasses the ORM's append-only hook (``aecom.db._audit_is_append_only``). Reopening works
only on the single newest closed month, always needs a reason, and is audited with the fingerprint the month had
before it was reopened so the trail of what changed stays visible.

No agent tool calls into this module: closing and reopening a period is human-only (enforced by there being no
``close``/``reopen`` entry in ``aecom.tools.catalog.CATALOG`` or ``policy.yaml`` - see ``test_periods.py``).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from aecom.audit import audit
from aecom.db import LedgerEntry, Period, now, session
from aecom.money import cents


class PeriodClosed(ValueError):
    """Raised when an operation needs a period that is not closed, but it is."""


def _month_bounds(period: str) -> tuple[datetime, datetime]:
    """(start, end) of the month, end exclusive. Also validates the 'YYYY-MM' format."""
    try:
        if len(period) != 7 or period[4] != "-":
            raise ValueError
        year, month = int(period[:4]), int(period[5:])
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        raise ValueError(f"invalid period {period!r}; expected 'YYYY-MM'") from None
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def _pretty(period: str | None) -> str:
    if not period:
        return "none"
    start, _ = _month_bounds(period)
    return start.strftime("%B %Y")


def status(period: str) -> str:
    """'open' | 'closed'. A month never closed (including one with no ledger lines yet) is 'open'."""
    _month_bounds(period)
    with session() as s:
        row = s.get(Period, period)
        return row.status if row else "open"


def is_past(period: str) -> bool:
    """True once the month has fully ended - the condition `close` requires before it will close it."""
    _, end = _month_bounds(period)
    return now() >= end


def is_closed(value: date | datetime | str) -> bool:
    """True if the month of ``value`` (a date/datetime, or already a 'YYYY-MM' string) is closed."""
    period = value if isinstance(value, str) else value.strftime("%Y-%m")
    return status(period) == "closed"


def _ledger_lines(s: Session, period: str):
    start, end = _month_bounds(period)
    return s.scalars(select(LedgerEntry).where(LedgerEntry.created_at >= start, LedgerEntry.created_at < end)
                     .order_by(LedgerEntry.id))


def _scan(s: Session, period: str) -> dict:
    """One pass over the month's ledger lines: its debit/credit totals (in cents, for the balance check) and its
    fingerprint (over txn_id, account, debit, credit, ref, country, vat_rate, created_at ISO - all in the order the
    rows were created)."""
    h = hashlib.sha256()
    debit = credit = lines = 0
    for e in _ledger_lines(s, period):
        debit += cents(e.debit)
        credit += cents(e.credit)
        lines += 1
        h.update("|".join([e.txn_id, e.account, str(cents(e.debit)), str(cents(e.credit)), e.ref or "",
                           e.country or "", "" if e.vat_rate is None else repr(e.vat_rate),
                           e.created_at.isoformat()]).encode())
    return {"debit": debit, "credit": credit, "lines": lines, "fingerprint": h.hexdigest()}


def ledger_fingerprint(period: str) -> str:
    """sha256 (hex) over that month's ledger lines, ordered by id. Amounts are cents integers so float noise never
    changes it; recomputing it later and comparing to what ``close`` stored is how ``verify`` catches tampering."""
    _month_bounds(period)
    with session() as s:
        return _scan(s, period)["fingerprint"]


def list_periods(n: int = 24) -> list[dict]:
    """Every month that has at least one ledger line, plus any month already recorded in ``ops_periods`` (so a
    closed month keeps showing even if, somehow, it ended up with no lines), newest first, capped at ``n``."""
    with session() as s:
        counts = Counter(dt.strftime("%Y-%m") for dt in s.scalars(select(LedgerEntry.created_at)))
        rows = {p.period: p for p in s.scalars(select(Period))}
    months = sorted(set(counts) | set(rows), reverse=True)[:n]
    out = []
    for period in months:
        row = rows.get(period)
        closed = bool(row and row.status == "closed")
        out.append({
            "period": period,
            "status": row.status if row else "open",
            "lines": row.ledger_lines if closed else counts.get(period, 0),
            "fingerprint": row.ledger_hash if closed else "",
            "closed_by": row.closed_by if row else "",
            "closed_at": row.closed_at if row else None,
            "reopened_by": row.reopened_by if row else "",
            "reopened_at": row.reopened_at if row else None,
            "note": row.note if row else "",
        })
    return out


def close(period: str, by: str, note: str = "") -> dict:
    """Close ``period``: refuses an already-closed month (``PeriodClosed``), a month that is not entirely in the
    past, an unbalanced month (debit != credit in cents), or closing out of order (an earlier month with ledger
    lines that is still open). Stores the fingerprint and line count, and audits ``period_close`` as
    ``human:<by>``."""
    start, end = _month_bounds(period)
    with session() as s:
        row = s.get(Period, period)
        if row is not None and row.status == "closed":
            raise PeriodClosed(f"{_pretty(period)} is already closed")
        if now() < end:  # same check as is_past(period), inlined to share `end` with the rest of this transaction
            raise ValueError(f"{_pretty(period)} is not over yet; it can only be closed once it has fully passed")
        earlier = {dt.strftime("%Y-%m") for dt in
                  s.scalars(select(LedgerEntry.created_at).where(LedgerEntry.created_at < start))}
        closed_months = {p.period for p in s.scalars(select(Period).where(Period.status == "closed"))}
        still_open = sorted(m for m in earlier if m not in closed_months)
        if still_open:
            raise ValueError(f"{_pretty(still_open[0])} must be closed first; months close in order")
        scan = _scan(s, period)
        if scan["debit"] != scan["credit"]:
            raise ValueError(f"{_pretty(period)} is unbalanced (debit {scan['debit'] / 100:.2f} vs. credit "
                             f"{scan['credit'] / 100:.2f} EUR) and cannot be closed")
        if row is None:
            row = Period(period=period)
            s.add(row)
        row.status, row.closed_by, row.closed_at = "closed", by, now()
        row.ledger_hash, row.ledger_lines, row.note = scan["fingerprint"], scan["lines"], note
        result = {"period": period, "status": "closed", "lines": scan["lines"], "fingerprint": scan["fingerprint"]}
    audit(f"human:{by}" if by else "system", "period_close", "executed",
          {"period": period, "lines": result["lines"], "fingerprint": result["fingerprint"], "note": note})
    return result


def reopen(period: str, by: str, reason: str) -> dict:
    """Reopen ``period``, which must be the single newest closed month (older months stay closed - post the
    correction once this month is closed again, in order). A reason is required and is audited loudly together
    with the fingerprint the month had before reopening, so later review sees both what changed and why."""
    _month_bounds(period)
    if not (reason or "").strip():
        raise ValueError("a reason is required to reopen a closed month")
    with session() as s:
        row = s.get(Period, period)
        if row is None or row.status != "closed":
            raise ValueError(f"{_pretty(period)} is not closed; there is nothing to reopen")
        newest = max((p.period for p in s.scalars(select(Period).where(Period.status == "closed"))), default=None)
        if period != newest:
            raise ValueError(f"only the newest closed month ({_pretty(newest)}) can be reopened; correct through "
                             "it, close it again, then reopen an older month if still needed")
        old_fingerprint, old_lines = row.ledger_hash, row.ledger_lines
        old_closed_by, old_closed_at = row.closed_by, row.closed_at
        row.status, row.reopened_by, row.reopened_at = "open", by, now()
    audit(f"human:{by}" if by else "system", "period_reopen", "executed",
          {"period": period, "reason": reason, "old_fingerprint": old_fingerprint, "old_lines": old_lines,
           "previously_closed_by": old_closed_by,
           "previously_closed_at": old_closed_at.isoformat() if old_closed_at else None})
    return {"period": period, "status": "open", "old_fingerprint": old_fingerprint}


def verify(period: str) -> dict:
    """Recompute the month's fingerprint and line count and compare to what ``close`` stored. ``ok`` is True for a
    month that was never closed (nothing to tamper with yet) or whose ledger still matches what was recorded;
    False means a closed month's rows were edited, deleted or backdated into it after the fact."""
    _month_bounds(period)
    with session() as s:
        row = s.get(Period, period)
        scan = _scan(s, period)
    expected, lines_expected = (row.ledger_hash, row.ledger_lines) if row else ("", 0)
    closed = bool(row and row.status == "closed")
    ok = (not closed) or (scan["fingerprint"] == expected and scan["lines"] == lines_expected)
    return {"ok": ok, "expected": expected, "actual": scan["fingerprint"], "lines_expected": lines_expected,
            "lines_actual": scan["lines"]}


def verify_all() -> list[dict]:
    """``verify`` for every closed month, oldest first."""
    with session() as s:
        closed = sorted(p.period for p in s.scalars(select(Period).where(Period.status == "closed")))
    return [{"period": p, **verify(p)} for p in closed]
