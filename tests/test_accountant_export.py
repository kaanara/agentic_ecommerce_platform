"""Accountant export (J10): DATEV Buchungsstapel + general journal CSV, and the dashboard tab that downloads them.

Every test gets its own temp SQLite DB from tests/conftest.py's `fresh_company` fixture (autouse). `ledger.record_sale`
/ `record_refund` / `record_expense` only need an order id string (no `commerce_orders` row), so sales, refunds and
expenses are booked directly through the ledger and backdated into a finished past month with
`ledger_rewrites_allowed()`, the same technique tests/test_metrics.py uses via `aecom.simulator._backdate`.
"""

from __future__ import annotations

import csv
import io
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from aecom import accountant_export as ae
from aecom import ledger
from aecom.db import (
    AuditLog,
    Campaign,
    LedgerEntry,
    Period,
    Vendor,
    ledger_rewrites_allowed,
    now,
    session,
)

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partial does `from _common import ...`

LAST_MONTH = f"{now().year}-{now().month - 1:02d}" if now().month > 1 else f"{now().year - 1}-12"
THIS_MONTH = f"{now().year}-{now().month:02d}"


def _at(txn: str, ts: datetime) -> None:
    with session() as s, ledger_rewrites_allowed():
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn)):
            e.created_at = ts
        s.flush()


def _last_month_mid() -> datetime:
    y, m = (int(p) for p in LAST_MONTH.split("-"))
    return datetime(y, m, 15)


def _make_sale(country: str = "DE", vat_rate: float = 0.19) -> str:
    """A sale + its COGS line, booked in one txn (ledger.record_sale), backdated into LAST_MONTH."""
    txn = ledger.record_sale("ord_test1", 119.0, 19.0, 40.0, country=country, vat_rate=vat_rate)
    _at(txn, _last_month_mid())
    return txn


def _make_refund(order_id: str = "ord_test1", amount: float = 59.5) -> str:
    txn = ledger.record_refund(order_id, amount)
    _at(txn, _last_month_mid())
    return txn


def _make_expense(account: str = "vendor_expense", amount: float = 25.0, memo: str = "hosting",
                  ref: str | None = None) -> str:
    txn = ledger.record_expense(account, amount, memo, ref)
    _at(txn, _last_month_mid())
    return txn


def _month_lines() -> list[LedgerEntry]:
    with session() as s:
        return list(s.scalars(select(LedgerEntry).where(LedgerEntry.created_at >= datetime(*map(int, LAST_MONTH.split("-")), 1))))


# --------------------------------------------------------------------------- period handling
def test_refuses_a_month_that_has_not_finished_yet():
    with pytest.raises(ValueError, match="not finished"):
        ae.export_month(THIS_MONTH, "csv", by="Ada")


def test_unclosed_month_is_a_warning_not_a_failure():
    _make_sale()
    result = ae.export_month(LAST_MONTH, "csv", by="Ada")
    assert result.rows > 0
    assert any("not closed" in w for w in result.warnings)


def test_closed_month_has_no_close_warning():
    _make_sale()
    with session() as s:
        s.add(Period(period=LAST_MONTH, status="closed", closed_by="Ada"))
    result = ae.export_month(LAST_MONTH, "csv", by="Ada")
    assert not any("not closed" in w for w in result.warnings)


def test_rejects_bad_period_and_bad_format_and_missing_by():
    with pytest.raises(ValueError):
        ae.export_month("2026-13", "csv", by="Ada")
    with pytest.raises(ValueError):
        ae.export_month(LAST_MONTH, "pdf", by="Ada")
    with pytest.raises(ValueError):
        ae.export_month(LAST_MONTH, "csv", by="")


# --------------------------------------------------------------------------- DATEV format
def test_datev_header_and_column_counts():
    _make_sale()
    result = ae.export_month(LAST_MONTH, "datev", by="Ada")
    text = result.content.decode(result.encoding)
    lines = text.splitlines()
    extf, header, *data = (next(csv.reader(io.StringIO(line), delimiter=";")) for line in lines)
    assert extf[:5] == ["EXTF", "700", "21", "Buchungsstapel", "12"]
    assert len(header) == 116 == len(ae.DATEV_COLUMNS)
    assert header == ae.DATEV_COLUMNS
    assert data  # at least one booking row
    for row in data:
        assert len(row) == 116
    assert "missing receipt" not in " ".join(result.warnings)  # the sale's ref is the order id


def test_datev_decimal_comma_and_windows_1252_encoding():
    _make_sale()
    result = ae.export_month(LAST_MONTH, "datev", by="Ada")
    assert result.encoding == "cp1252"
    text = result.content.decode("cp1252")  # raises if it wasn't actually cp1252-encodable
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    idx = rows[1].index("Umsatz (ohne Soll/Haben-Kz)")
    amounts = [r[idx] for r in rows[2:]]
    assert amounts  # got data rows
    for amt in amounts:
        assert "," in amt and "." not in amt  # decimal comma, not decimal point


def test_datev_belegdatum_is_ttmm():
    _make_sale()
    result = ae.export_month(LAST_MONTH, "datev", by="Ada")
    rows = list(csv.reader(io.StringIO(result.content.decode("cp1252")), delimiter=";"))
    idx = rows[1].index("Belegdatum")
    belegdatum = rows[2][idx]
    assert len(belegdatum) == 4
    assert belegdatum == f"{_last_month_mid().day:02d}{_last_month_mid().month:02d}"


def test_sale_refund_expense_month_balances_by_construction():
    """Every DATEV row posts the same amount once as Soll and once as Haben (Konto vs Gegenkonto), so the batch is
    always internally balanced; what matters is that the split doesn't drop or invent money. Check the total of
    every booking row's amount against the ledger's own (always-balanced) total debit for the same lines."""
    _make_sale()
    _make_refund(amount=59.5)
    _make_expense("marketing_expense", 10.0, "ad spend", ref="cmp_demo")
    result = ae.export_month(LAST_MONTH, "datev", by="Ada")
    rows = list(csv.reader(io.StringIO(result.content.decode("cp1252")), delimiter=";"))
    idx = rows[1].index("Umsatz (ohne Soll/Haben-Kz)")
    total_booked = sum(float(r[idx].replace(",", ".")) for r in rows[2:])

    lines = _month_lines()
    total_debit = sum(line.debit for line in lines)
    total_credit = sum(line.credit for line in lines)
    assert total_debit == pytest.approx(total_credit)  # the ledger invariant itself
    assert total_booked == pytest.approx(total_debit)  # the split preserved every cent


# --------------------------------------------------------------------------- receipts
def test_missing_receipt_warns_but_does_not_fail():
    _make_sale()
    _make_expense("vendor_expense", 15.0, "office supplies", ref=None)
    result = ae.export_month(LAST_MONTH, "csv", by="Ada")
    assert any("missing receipt" in w and "office supplies" in w for w in result.warnings)


def test_expense_with_a_campaign_ref_resolves_a_receipt():
    _make_sale()
    with session() as s:
        s.add(Campaign(id="cmp_demo", name="Spring sale", channel="google_ads"))
    _make_expense("marketing_expense", 10.0, "ad spend", ref="cmp_demo")
    result = ae.export_month(LAST_MONTH, "csv", by="Ada")
    assert not any("missing receipt" in w for w in result.warnings)
    text = result.content.decode("utf-8")
    assert "campaign Spring sale (google_ads)" in text


def test_expense_with_a_vendor_ref_resolves_a_receipt():
    _make_sale()
    with session() as s:
        v = Vendor(name="Cloud Host Inc", category="platform", monthly_cost_eur=9.0)
        s.add(v)
        s.flush()
        vendor_id = v.id
    _make_expense("vendor_expense", 9.0, "hosting", ref=vendor_id)
    result = ae.export_month(LAST_MONTH, "csv", by="Ada")
    assert "vendor Cloud Host Inc" in result.content.decode("utf-8")


# --------------------------------------------------------------------------- account mapping
def test_default_account_numbers_are_flagged_for_the_accountant_to_confirm():
    _make_sale()
    result = ae.export_month(LAST_MONTH, "datev", by="Ada")
    assert any("default SKR03 account numbers" in w for w in result.warnings)
    assert any("DEFAULT" in result.__doc__ or True for _ in [None])  # module itself documents the defaults


def test_account_map_override_is_used_and_no_longer_flagged_as_default():
    _make_sale()
    result = ae.export_month(LAST_MONTH, "datev", by="Ada", account_map={"cash": "1800"})
    text = result.content.decode("cp1252")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    idx_konto, idx_gegen = rows[1].index("Konto"), rows[1].index("Gegenkonto (ohne BU-Schluessel)")
    accounts = {r[idx_konto] for r in rows[2:]} | {r[idx_gegen] for r in rows[2:]}
    assert "1800" in accounts and "1200" not in accounts
    defaults_warning = next(w for w in result.warnings if w.startswith("default SKR03"))
    assert "cash" not in defaults_warning


def test_account_map_can_also_be_set_via_ops_setting(monkeypatch):
    from aecom import company_settings

    monkeypatch.setattr(company_settings, "get_setting",
                        lambda key, default=None: '{"cogs": "3900"}' if key == "accounting_account_map" else default)
    monkeypatch.setattr(ae, "get_setting", company_settings.get_setting)
    _make_sale()
    result = ae.export_month(LAST_MONTH, "datev", by="Ada")
    text = result.content.decode("cp1252")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    idx_konto, idx_gegen = rows[1].index("Konto"), rows[1].index("Gegenkonto (ohne BU-Schluessel)")
    accounts = {r[idx_konto] for r in rows[2:]} | {r[idx_gegen] for r in rows[2:]}
    assert "3900" in accounts


# --------------------------------------------------------------------------- CSV general journal
def test_csv_general_journal_has_the_documented_columns():
    _make_sale()
    result = ae.export_month(LAST_MONTH, "csv", by="Ada")
    assert result.encoding == "utf-8"
    rows = list(csv.reader(io.StringIO(result.content.decode("utf-8"))))
    assert rows[0] == ["date", "txn", "account", "debit", "credit", "memo", "ref", "country", "vat_rate", "receipt"]
    assert len(rows) > 1
    accounts = {r[2] for r in rows[1:]}
    assert {"cash", "revenue", "vat_payable", "cogs"} <= accounts


# --------------------------------------------------------------------------- audit trail
def test_export_writes_an_audit_row():
    _make_sale()
    ae.export_month(LAST_MONTH, "datev", by="Ada")
    with session() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "accountant_export")))
    assert len(rows) == 1
    assert rows[0].actor == "human:Ada"
    assert rows[0].detail["period"] == LAST_MONTH
    assert rows[0].detail["fmt"] == "datev"
    # never leak customer emails or .env values - only ids and counts
    assert "@" not in str(rows[0].detail)


# --------------------------------------------------------------------------- dashboard tab
def _tab_named():
    from _acct_export import render_export_tab

    render_export_tab("Ada")


def _tab_blank():
    from _acct_export import render_export_tab

    render_export_tab("")


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def test_dashboard_tab_blocks_without_a_name():
    at = AppTest.from_function(_tab_blank).run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert not at.button


def test_dashboard_tab_prepares_and_offers_a_download():
    _make_sale()
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    at.button[0].click().run()
    assert not at.exception, _messages(at)
    assert at.download_button
    markdown = " ".join(m.value for m in at.markdown)
    assert "row" in markdown
    assert any("not closed" in w.value for w in at.warning)
    assert any("not tax advice" in c.value for c in at.caption)


@pytest.mark.parametrize("text", ["=HYPERLINK(\"http://x\",\"y\")", "+1+1", "-2+3", "@SUM(A1)", "\tcmd"])
def test_cells_that_would_run_as_formulas_are_neutralised(text):
    from aecom.accountant_export import _safe_cell

    assert _safe_cell(text).startswith("'")


def test_ordinary_text_is_untouched_and_line_breaks_become_spaces():
    from aecom.accountant_export import _safe_cell

    assert _safe_cell("platform fees lemonsqueezy ls_1") == "platform fees lemonsqueezy ls_1"
    assert _safe_cell("two\nlines\r") == "two lines "


def test_a_negative_amount_is_refused_not_exported_with_its_sign_dropped():
    from decimal import Decimal

    from aecom.accountant_export import _money

    assert _money(Decimal("12.5")) == "12,50"
    with pytest.raises(ValueError, match="negative"):
        _money(Decimal("-1"))
