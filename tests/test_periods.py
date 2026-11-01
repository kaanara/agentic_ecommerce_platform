"""Period close (J8) and ledger immutability (J11): tests/test_periods.py.

Months are expressed relative to the real current month (`_shift`) so these tests keep working whenever they run,
without hardcoding dates. Ledger lines are booked through `ledger.record_sale` (so they are balanced and carry a
real txn_id) and then backdated into the target month with `ledger_rewrites_allowed()` - the same technique
`tests/test_metrics.py` and `tests/test_accountant_export.py` use via `aecom.simulator._backdate`: `ledger.post`
only ever timestamps an entry "now", so a month only ever gets ledger lines dated inside it by moving them there
afterwards (real production code, i.e. the simulator, backdates this same way).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, update
from streamlit.testing.v1 import AppTest

from aecom import ledger, periods
from aecom.config import get_settings
from aecom.db import AuditLog, LedgerEntry, Period, get_engine, ledger_rewrites_allowed, now, session
from aecom.tools.catalog import CATALOG

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partial does `from _common import ...`


def _shift(n: int) -> str:
    """'YYYY-MM' n months before the current month (n=0 is this month)."""
    y, m = now().year, now().month
    total = y * 12 + (m - 1) - n
    return f"{total // 12}-{total % 12 + 1:02d}"


THIS_MONTH = _shift(0)
LAST_MONTH = _shift(1)
TWO_MONTHS_AGO = _shift(2)
THREE_MONTHS_AGO = _shift(3)


def _pretty(period: str) -> str:
    y, m = (int(x) for x in period.split("-"))
    return datetime(y, m, 1).strftime("%B %Y")


def _mid(period: str) -> datetime:
    y, m = (int(x) for x in period.split("-"))
    return datetime(y, m, 15)


def _at(txn: str, ts: datetime) -> None:
    with session() as s, ledger_rewrites_allowed():
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)):
            e.created_at = ts
        s.flush()


def _book(period: str, order_id: str | None = None, net: float = 100.0, vat: float = 19.0) -> str:
    """A balanced sale (net + vat in cash/revenue/vat_payable), backdated into the middle of `period`."""
    order_id = order_id or f"ord_{period}_{net}_{vat}".replace(".", "_")
    txn = ledger.record_sale(order_id, net + vat, vat, 0.0)
    _at(txn, _mid(period))
    return txn


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


# --------------------------------------------------------------------------- status / is_closed / is_past
def test_status_is_open_for_a_month_never_touched():
    assert periods.status(TWO_MONTHS_AGO) == "open"
    assert periods.is_closed(TWO_MONTHS_AGO) is False


def test_status_and_is_past_validate_the_period_format():
    with pytest.raises(ValueError, match="invalid period"):
        periods.status("2026/01")
    with pytest.raises(ValueError, match="invalid period"):
        periods.is_past("not-a-period")


def test_is_past_true_only_once_the_month_has_fully_ended():
    assert periods.is_past(TWO_MONTHS_AGO) is True
    assert periods.is_past(THIS_MONTH) is False


# --------------------------------------------------------------------------- close: happy path + fingerprint
def test_close_stores_fingerprint_lines_and_audits():
    _book(TWO_MONTHS_AGO, net=100.0, vat=19.0)
    _book(TWO_MONTHS_AGO, net=50.0, vat=9.5)
    expected_fp = periods.ledger_fingerprint(TWO_MONTHS_AGO)

    result = periods.close(TWO_MONTHS_AGO, "Ada", note="first close")

    assert result == {"period": TWO_MONTHS_AGO, "status": "closed", "lines": result["lines"],
                      "fingerprint": expected_fp}
    assert result["lines"] > 0
    assert periods.status(TWO_MONTHS_AGO) == "closed"
    assert periods.is_closed(TWO_MONTHS_AGO) is True
    assert periods.is_closed(_mid(TWO_MONTHS_AGO)) is True
    assert periods.is_closed(_mid(TWO_MONTHS_AGO).date()) is True

    with session() as s:
        row = s.get(Period, TWO_MONTHS_AGO)
        assert row.ledger_hash == expected_fp
        assert row.ledger_lines == result["lines"]
        assert row.closed_by == "Ada"
        assert row.note == "first close"
        assert row.closed_at is not None

    with session() as s:
        logs = list(s.scalars(select(AuditLog).where(AuditLog.action == "period_close")))
    assert len(logs) == 1
    assert logs[0].actor == "human:Ada"
    assert logs[0].detail["period"] == TWO_MONTHS_AGO
    assert logs[0].detail["fingerprint"] == expected_fp
    assert logs[0].detail["note"] == "first close"


def test_ledger_fingerprint_is_stable_and_changes_with_content():
    _book(TWO_MONTHS_AGO)
    fp1 = periods.ledger_fingerprint(TWO_MONTHS_AGO)
    assert fp1 == periods.ledger_fingerprint(TWO_MONTHS_AGO)  # deterministic, no side effects
    _book(TWO_MONTHS_AGO, net=5.0, vat=0.95)
    assert periods.ledger_fingerprint(TWO_MONTHS_AGO) != fp1


# --------------------------------------------------------------------------- close: refusals
def test_close_refuses_a_month_not_fully_in_the_past():
    with pytest.raises(ValueError, match="not over yet"):
        periods.close(THIS_MONTH, "Ada")
    assert periods.status(THIS_MONTH) == "open"


def test_close_refuses_an_already_closed_month():
    _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada")
    with pytest.raises(periods.PeriodClosed, match="already closed"):
        periods.close(TWO_MONTHS_AGO, "Ada")


def test_close_refuses_out_of_order():
    _book(TWO_MONTHS_AGO)
    _book(THREE_MONTHS_AGO)
    with pytest.raises(ValueError, match="months close in order"):
        periods.close(TWO_MONTHS_AGO, "Ada")
    assert periods.status(TWO_MONTHS_AGO) == "open"
    # closing the earlier one first unblocks it
    periods.close(THREE_MONTHS_AGO, "Ada")
    periods.close(TWO_MONTHS_AGO, "Ada")
    assert periods.status(TWO_MONTHS_AGO) == "closed"


def test_close_refuses_an_unbalanced_month():
    with session() as s:  # a lone debit line: no matching credit anywhere (fresh insert, not an edit)
        s.add(LedgerEntry(txn_id="txn_bad", account="cash", debit=10.0, credit=0.0, created_at=_mid(TWO_MONTHS_AGO)))
    with pytest.raises(ValueError, match="unbalanced"):
        periods.close(TWO_MONTHS_AGO, "Ada")
    assert periods.status(TWO_MONTHS_AGO) == "open"


# --------------------------------------------------------------------------- ledger.post refuses a closed period
def test_post_into_a_closed_current_month_is_refused():
    with session() as s:
        s.add(Period(period=THIS_MONTH, status="closed", closed_by="Ada"))
    with pytest.raises(periods.PeriodClosed, match=f"{_pretty(THIS_MONTH)} is closed"):
        ledger.post([("cash", 10, 0), ("revenue", 0, 10)], "blocked")
    with session() as s:
        assert list(s.scalars(select(LedgerEntry).where(LedgerEntry.memo == "blocked"))) == []  # nothing posted


def test_record_expense_also_refuses_a_closed_current_month():
    with session() as s:
        s.add(Period(period=THIS_MONTH, status="closed", closed_by="Ada"))
    with pytest.raises(periods.PeriodClosed):
        ledger.record_expense("vendor_expense", 5.0, "hosting")


# --------------------------------------------------------------------------- reopen
def test_reopen_only_the_newest_closed_month():
    _book(THREE_MONTHS_AGO)
    _book(TWO_MONTHS_AGO)
    periods.close(THREE_MONTHS_AGO, "Ada")
    periods.close(TWO_MONTHS_AGO, "Ada")

    with pytest.raises(ValueError, match="newest closed month"):
        periods.reopen(THREE_MONTHS_AGO, "Ada", "correction needed")

    result = periods.reopen(TWO_MONTHS_AGO, "Ada", "correction needed")
    assert result == {"period": TWO_MONTHS_AGO, "status": "open", "old_fingerprint": result["old_fingerprint"]}
    assert periods.status(TWO_MONTHS_AGO) == "open"
    assert periods.status(THREE_MONTHS_AGO) == "closed"  # untouched


def test_reopen_requires_a_non_blank_reason():
    _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada")
    with pytest.raises(ValueError, match="reason"):
        periods.reopen(TWO_MONTHS_AGO, "Ada", "")
    with pytest.raises(ValueError, match="reason"):
        periods.reopen(TWO_MONTHS_AGO, "Ada", "   ")
    assert periods.status(TWO_MONTHS_AGO) == "closed"  # neither attempt touched it


def test_reopen_refuses_a_month_that_is_not_closed():
    with pytest.raises(ValueError, match="not closed"):
        periods.reopen(TWO_MONTHS_AGO, "Ada", "oops")


def test_reopen_is_audited_loudly_with_the_old_fingerprint():
    _book(TWO_MONTHS_AGO)
    closed = periods.close(TWO_MONTHS_AGO, "Ada")
    periods.reopen(TWO_MONTHS_AGO, "Ada", "found a missing invoice")

    with session() as s:
        logs = list(s.scalars(select(AuditLog).where(AuditLog.action == "period_reopen")))
    assert len(logs) == 1
    row = logs[0]
    assert row.actor == "human:Ada"
    assert row.detail["period"] == TWO_MONTHS_AGO
    assert row.detail["reason"] == "found a missing invoice"
    assert row.detail["old_fingerprint"] == closed["fingerprint"]
    assert row.detail["previously_closed_by"] == "Ada"


# --------------------------------------------------------------------------- verify / verify_all (tamper detection)
def test_verify_ok_for_a_month_that_was_never_closed():
    _book(TWO_MONTHS_AGO)
    result = periods.verify(TWO_MONTHS_AGO)
    assert result["ok"] is True
    assert result["expected"] == ""


def test_verify_ok_right_after_closing():
    _book(TWO_MONTHS_AGO)
    closed = periods.close(TWO_MONTHS_AGO, "Ada")
    result = periods.verify(TWO_MONTHS_AGO)
    assert result == {"ok": True, "expected": closed["fingerprint"], "actual": closed["fingerprint"],
                      "lines_expected": result["lines_expected"], "lines_actual": result["lines_expected"]}


def test_verify_detects_a_raw_sql_update_of_a_closed_months_row():
    """A raw SQLAlchemy Core UPDATE goes straight to the engine, bypassing the ORM Session the append-only
    `before_flush` hook listens on - this is exactly the kind of tampering the fingerprint exists to catch."""
    txn = _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada")
    assert periods.verify(TWO_MONTHS_AGO)["ok"] is True

    with get_engine().begin() as conn:
        conn.execute(update(LedgerEntry.__table__).where(LedgerEntry.txn_id == txn, LedgerEntry.account == "cash")
                    .values(debit=9999.0))

    result = periods.verify(TWO_MONTHS_AGO)
    assert result["ok"] is False
    assert result["actual"] != result["expected"]
    assert result["lines_actual"] == result["lines_expected"]  # same row count, different content


def test_verify_detects_a_backdated_insert_into_a_closed_month():
    _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada")
    assert periods.verify(TWO_MONTHS_AGO)["ok"] is True

    with session() as s:  # a fresh insert - the append-only hook only guards edits/deletes, not new rows
        s.add(LedgerEntry(txn_id="txn_sneaky", account="cash", debit=5.0, credit=0.0,
                          created_at=_mid(TWO_MONTHS_AGO)))

    result = periods.verify(TWO_MONTHS_AGO)
    assert result["ok"] is False
    assert result["lines_actual"] == result["lines_expected"] + 1


def test_verify_all_lists_every_closed_month_oldest_first():
    _book(THREE_MONTHS_AGO)
    _book(TWO_MONTHS_AGO)
    periods.close(THREE_MONTHS_AGO, "Ada")
    periods.close(TWO_MONTHS_AGO, "Ada")

    results = periods.verify_all()
    assert [r["period"] for r in results] == [THREE_MONTHS_AGO, TWO_MONTHS_AGO]
    assert all(r["ok"] for r in results)


# --------------------------------------------------------------------------- list_periods
def test_list_periods_reports_status_lines_and_fingerprint():
    _book(TWO_MONTHS_AGO)
    _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada", note="done")

    rows = {r["period"]: r for r in periods.list_periods()}
    assert rows[TWO_MONTHS_AGO]["status"] == "closed"
    assert rows[TWO_MONTHS_AGO]["lines"] > 0
    assert rows[TWO_MONTHS_AGO]["fingerprint"] == periods.ledger_fingerprint(TWO_MONTHS_AGO)
    assert rows[TWO_MONTHS_AGO]["closed_by"] == "Ada"
    assert rows[TWO_MONTHS_AGO]["note"] == "done"


def test_list_periods_caps_at_n():
    for i in range(1, 6):
        _book(_shift(i))
    assert len(periods.list_periods(n=3)) == 3


# --------------------------------------------------------------------------- append-only ledger (J11)
def test_append_only_hook_blocks_orm_edit_and_delete():
    txn = _book(TWO_MONTHS_AGO)
    with pytest.raises(PermissionError, match="append-only"):
        with session() as s:
            e = s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)).first()
            e.debit = 9999.0
    with pytest.raises(PermissionError, match="append-only"):
        with session() as s:
            e = s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)).first()
            s.delete(e)


def test_ledger_rewrites_allowed_bypasses_the_hook():
    from aecom.db import ledger_rewrites_allowed as bypass

    txn = _book(TWO_MONTHS_AGO)
    with session() as s, bypass():
        e = s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)).first()
        e.debit = 42.0
        s.flush()  # while the bypass is still active (it resets on __exit__, before session()'s own commit flush)
    with session() as s:
        e = s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)).first()
        assert e.debit == 42.0


# --------------------------------------------------------------------------- no agent tool can close/reopen
def test_no_agent_tool_can_close_or_reopen_a_period():
    suspicious_tools = {n for n in CATALOG if "period" in n.lower() and ("close" in n.lower() or "reopen" in n.lower())}
    assert not suspicious_tools, f"an agent tool could close/reopen a period: {suspicious_tools}"

    policy = yaml.safe_load(get_settings().policy_file.read_text())
    rule_names = set((policy.get("rules") or {}).keys())
    suspicious_rules = {n for n in rule_names if "period" in n.lower() and ("close" in n.lower() or "reopen" in n.lower())}
    assert not suspicious_rules, f"policy.yaml grants a rule for: {suspicious_rules}"


# --------------------------------------------------------------------------- dashboard tab (AppTest)
def _periods_tab_named():
    from _acct_periods import render_periods_tab

    render_periods_tab("Ada")


def test_periods_tab_empty_state_with_no_ledger_activity():
    at = AppTest.from_function(_periods_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("No ledger activity yet" in m.value for m in at.markdown)


def test_periods_tab_table_and_close_offered_for_a_finished_month():
    _book(TWO_MONTHS_AGO)
    at = AppTest.from_function(_periods_tab_named).run()
    assert not at.exception, _messages(at)
    assert at.dataframe  # the months table
    assert at.selectbox(key="_period_close_select").options == [TWO_MONTHS_AGO]
    assert at.button(key="_period_close_btn")
    assert any("No month is closed yet" in c.value for c in at.caption)  # nothing to reopen yet


def test_periods_tab_close_opens_confirm_dialog():
    _book(TWO_MONTHS_AGO)
    at = AppTest.from_function(_periods_tab_named).run()
    at.button(key="_period_close_btn").click().run()
    assert not at.exception, _messages(at)
    # the confirm_dialog modal is now open (not completable further: AppTest does not simulate st.dialog
    # persisting across a second .run(), the same limitation noted in test_dashboard_settings.py)
    assert any("Nothing more can be posted into it" in m.value for m in at.markdown)
    assert periods.status(TWO_MONTHS_AGO) == "open"  # nothing applied yet


def test_periods_tab_verify_all_reports_a_match():
    _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada")
    at = AppTest.from_function(_periods_tab_named).run()
    at.button(key="_period_verify_all_btn").click().run()
    assert not at.exception, _messages(at)
    assert any("match their recorded fingerprint" in s.value for s in at.success)


def test_periods_tab_reopen_needs_the_dashboard_login_configured_first():
    _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada")
    at = AppTest.from_function(_periods_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("Turn on the dashboard login" in w.value for w in at.warning)


def test_periods_tab_reopen_shows_the_stepup_form_once_login_is_configured():
    from aecom import dashboard_auth as auth

    auth.reset_password_local("a long test secret")
    _book(TWO_MONTHS_AGO)
    periods.close(TWO_MONTHS_AGO, "Ada")
    at = AppTest.from_function(_periods_tab_named).run()
    assert not at.exception, _messages(at)
    assert at.text_input(key="_period_reopen_stepup_pw")
    assert not any(b.key == "_period_reopen_btn" for b in at.button)  # not offered before step-up


def test_bulk_update_and_delete_of_ledger_rows_through_a_session_are_refused(fresh_company):
    from sqlalchemy import delete, update

    from aecom import ledger
    from aecom.db import LedgerEntry, ledger_rewrites_allowed, session

    ledger.record_expense("vendor_expense", 5, "test expense")
    with session() as s:
        with pytest.raises(PermissionError, match="append-only"):
            s.execute(update(LedgerEntry).values(debit=999))
    with session() as s:
        with pytest.raises(PermissionError, match="append-only"):
            s.execute(delete(LedgerEntry))
    with session() as s, ledger_rewrites_allowed():  # the simulator's escape hatch still works
        s.execute(update(LedgerEntry).values(memo="demo"))
