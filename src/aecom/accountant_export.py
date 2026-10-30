"""Accountant export (J10): a general journal for one month, handed to a German accountant.

Two formats:

* ``csv``   - a plain general journal, one row per raw ledger line (debit, credit, memo, ref, VAT, receipt).
              Dot-decimal, UTF-8: meant to open in any spreadsheet, not DATEV-specific.
* ``datev`` - a DATEV "Buchungsstapel" (EXTF, data category 21, format name "Buchungsstapel", format version 12)
              file: semicolon-separated, Windows-1252, decimal comma, ``TTMM`` Belegdatum. DATEV books single-sided
              (one ``Konto`` + one ``Gegenkonto`` per row, with a Soll/Haben-Kennzeichen), so our double-entry
              lines are grouped by ``txn_id`` and split into Konto/Gegenkonto pairs (see ``_split_txn``).

DATEV column list - **honesty notice**: the 116-column "Buchungsstapel" header below is reconstructed from public
DATEV format documentation reviewed during training, not copied from a verified current DATEV PDF in this session.
The core import columns this module actually fills (Umsatz, Soll/Haben-Kennzeichen, WKZ Umsatz, Konto, Gegenkonto,
BU-Schluessel, Belegdatum, Belegfeld 1, Buchungstext) are the ones consistently documented as required for a
Buchungsstapel import and are the ones to check first. The remaining columns are left empty padding so the column
*count* matches the documented format version 12 (116 columns); their exact names may be off by a column or two
versus the accountant's current DATEV release. **This export is a draft, never a certified DATEV file.** It must be
validated (a test import, or review against the accountant's own column mask) before real use - see
``docs/wiki/Accounting-Export.md``.

Account numbers are a default SKR03 chart mapping (``DEFAULT_ACCOUNT_MAP``) that the accountant must confirm; it can
be overridden per call (``account_map=``) or company-wide via the ops_setting ``accounting_account_map`` (JSON),
read with ``company_settings.get_setting`` - no schema change.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aecom.audit import audit
from aecom.company_settings import get_setting
from aecom.config import get_settings
from aecom.db import AgentRun, Campaign, LedgerEntry, Period, Vendor, now, session
from aecom.ledger import EXPENSE_ACCOUNTS
from aecom.money import to_decimal

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
FORMATS = ("datev", "csv")

# SKR03 account numbers. Every value here is a DEFAULT the accountant must confirm before the first real import.
DEFAULT_ACCOUNT_MAP: dict[str, str] = {
    "cash": "1200",              # Bank
    "revenue_19": "8400",        # domestic sales, 19% USt
    "revenue_7": "8300",         # domestic sales, 7% USt (reduced rate)
    "revenue_oss": "8120",       # EU cross-border / OSS sales (generic placeholder; override per country below)
    "vat_19": "1776",            # Umsatzsteuer 19% payable
    "vat_7": "1771",             # Umsatzsteuer 7% payable
    "vat_oss": "1767",           # OSS Umsatzsteuer payable (generic placeholder)
    "cogs": "3400",              # Wareneinsatz
    "marketing_expense": "4610", # Werbekosten
    "ai_expense": "4964",        # Kosten fuer EDV / Software (closest standard SKR03 slot)
    "vendor_expense": "4900",    # sonstige betriebliche Aufwendungen
    "refunds": "8700",           # Erloesschmaelerungen
}

VAT_RATE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class ExportResult:
    filename: str
    content: bytes
    rows: int
    warnings: list[str] = field(default_factory=list)
    encoding: str = "utf-8"


def _period_bounds(period: str) -> tuple[datetime, datetime, int, int]:
    if not PERIOD_RE.match(period):
        raise ValueError(f"period must be 'YYYY-MM', got {period!r}")
    year, month = int(period[:4]), int(period[5:7])
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end, year, month


def _account_map(override: dict[str, str] | None) -> tuple[dict[str, str], set[str]]:
    """``DEFAULT_ACCOUNT_MAP`` < the ``accounting_account_map`` ops setting (JSON) < the ``override`` argument.

    Returns ``(merged map, keys that were explicitly set by the setting or the argument)`` - those are treated as
    already confirmed by whoever configured them, so only the untouched SKR03 defaults get the "confirm with your
    accountant" warning.
    """
    merged = dict(DEFAULT_ACCOUNT_MAP)
    overridden: set[str] = set()
    raw = get_setting("accounting_account_map")
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            merged.update({str(k): str(v) for k, v in parsed.items()})
            overridden.update(str(k) for k in parsed)
    if override:
        merged.update({str(k): str(v) for k, v in override.items()})
        overridden.update(str(k) for k in override)
    return merged, overridden


def _rate_bucket(rate: float | None) -> str:
    if rate is not None and abs(rate - 0.19) < VAT_RATE_TOLERANCE:
        return "19"
    if rate is not None and abs(rate - 0.07) < VAT_RATE_TOLERANCE:
        return "7"
    return "oss"


def _konto(account: str, country: str | None, rate: float | None, amap: dict[str, str],
          overridden: set[str]) -> tuple[str, bool]:
    """(account number, is_default) for one ledger account in the given VAT context.

    ``is_default`` is True only for a number still at its SKR03 default - one explicitly set via
    ``accounting_account_map`` or the ``account_map`` argument is treated as already confirmed.
    """
    if account in ("revenue", "vat_payable"):
        prefix = "revenue" if account == "revenue" else "vat"
        bucket = _rate_bucket(rate)
        key = f"{prefix}_{bucket}"
        if bucket == "oss" and country and f"{prefix}_oss:{country}" in amap:
            key = f"{prefix}_oss:{country}"
        return amap[key], key not in overridden
    key = account if account in amap else "vendor_expense"  # unknown account: safest default bucket
    return amap[key], key not in overridden


def _receipt_for(s: Session, ref: str | None) -> str | None:
    """Best available receipt text for a ledger ``ref`` (campaign/vendor/order/agent-run id), or ``None``."""
    if not ref:
        return None
    if ref.startswith("cmp_"):
        c = s.get(Campaign, ref)
        if c:
            return f"campaign {c.name} ({c.channel})"
    elif ref.startswith("ven_"):
        v = s.get(Vendor, ref)
        if v:
            return f"vendor {v.name}"
    elif ref.startswith("run_"):
        r = s.get(AgentRun, ref)
        if r:
            return f"agent run {ref} ({r.agent_id})"
    elif ref.startswith("ord_"):
        return f"order {ref}"
    return ref  # no richer lookup matched: the raw id is still a usable document reference


def _split_txn(lines: list[LedgerEntry]) -> list[tuple[LedgerEntry, LedgerEntry, Decimal]]:
    """Split one balanced multi-line transaction into single Konto/Gegenkonto (debit, credit) pairs.

    Deterministic "waterfall" rule: list the transaction's debit lines in ledger-insertion order and its credit
    lines in ledger-insertion order (both already sum to the same total - the ledger enforces that). Walk both
    lists together; at each step take the smaller of the two amounts still unconsumed, emit one pair
    ``(debit_line, credit_line, amount)`` for it, and subtract that amount from both sides. Repeat until both
    lists are exhausted. E.g. a sale (cash 200 debit vs revenue 164 + vat 36 credit) yields
    ``(cash, revenue, 164)`` then ``(cash, vat, 36)``; a sale with COGS adds a third pair ``(cogs, cash, 120)``.
    This always balances (every pair debits and credits the same amount) and never drops or duplicates money,
    because the two queues start with equal totals and every step consumes equally from both.
    """
    debits = [[e, to_decimal(e.debit)] for e in lines if to_decimal(e.debit) > 0]
    credits = [[e, to_decimal(e.credit)] for e in lines if to_decimal(e.credit) > 0]
    pairs: list[tuple[LedgerEntry, LedgerEntry, Decimal]] = []
    di = ci = 0
    while di < len(debits) and ci < len(credits):
        d_entry, d_amt = debits[di]
        c_entry, c_amt = credits[ci]
        amt = min(d_amt, c_amt)
        pairs.append((d_entry, c_entry, amt))
        debits[di][1] -= amt
        credits[ci][1] -= amt
        if debits[di][1] <= 0:
            di += 1
        if credits[ci][1] <= 0:
            ci += 1
    return pairs


def _txns_in_period(s: Session, start: datetime, end: datetime) -> dict[str, list[LedgerEntry]]:
    rows = s.scalars(select(LedgerEntry).where(LedgerEntry.created_at >= start, LedgerEntry.created_at < end)
                     .order_by(LedgerEntry.created_at, LedgerEntry.id)).all()
    txns: dict[str, list[LedgerEntry]] = {}
    for e in rows:
        txns.setdefault(e.txn_id, []).append(e)
    return txns


def _period_warnings(period: str, start: datetime, end: datetime) -> list[str]:
    if now() < end:
        raise ValueError(f"{period} is not finished yet (it ends {end:%Y-%m-%d}); export an earlier month")
    warnings: list[str] = []
    with session() as s:
        row = s.get(Period, period)
    if row is None or row.status != "closed":
        warnings.append(f"{period} is not closed in ops_periods: treat this export as provisional until month-end "
                        "close (Govern > Accounting > Months)")
    return warnings


DATEV_DISCLAIMER = ("DATEV export is an unverified draft: the column layout follows the publicly documented EXTF "
                    "Buchungsstapel format version 12 from training knowledge, not a column-by-column check against "
                    "the accountant's current DATEV release. Account numbers are SKR03 defaults. Have the "
                    "accountant validate (or trial-import) before relying on this file.")

# Buchungsstapel v12 column header - see the module docstring's honesty notice. Core columns this module fills
# are listed first by name; the trailing columns are the documented standard ones, left empty.
DATEV_COLUMNS: list[str] = [
    "Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben-Kennzeichen", "WKZ Umsatz", "Kurs", "Basis-Umsatz",
    "WKZ Basis-Umsatz", "Konto", "Gegenkonto (ohne BU-Schluessel)", "BU-Schluessel", "Belegdatum", "Belegfeld 1",
    "Belegfeld 2", "Skonto", "Buchungstext", "Postensperre", "Diverse Adressnummer", "Geschaeftspartnerbank",
    "Sachverhalt", "Zinssperre", "Beleglink",
] + [f"Beleginfo - {kind} {n}" for n in range(1, 9) for kind in ("Art", "Inhalt")] + [
    "KOST1 - Kostenstelle", "KOST2 - Kostenstelle", "Kost-Menge", "EU-Land u. UStID", "EU-Steuersatz",
    "Abw. Versteuerungsart", "Sachverhalt L+L", "Funktionsergaenzung L+L", "BU 49 Hauptfunktionstyp",
    "BU 49 Hauptfunktionsnummer", "BU 49 Funktionsergaenzung",
] + [f"Zusatzinformation - {kind} {n}" for n in range(1, 21) for kind in ("Art", "Inhalt")] + [
    "Stueck", "Gewicht", "Zahlweise", "Forderungsart", "Veranlagungsjahr", "Zugeordnete Faelligkeit", "Skontotyp",
    "Auftragsnummer", "Buchungstyp", "USt-Schluessel (Anzahlungen)", "EU-Land (Anzahlungen)",
    "Sachverhalt L+L (Anzahlungen)", "EU-Steuersatz (Anzahlungen)", "Erloeskonto (Anzahlungen)", "Herkunft-Kz",
    "Buchungs GUID", "KOST-Datum", "SEPA-Mandatsreferenz", "Skontosperre", "Gesellschafternamen",
    "Beteiligtennummer", "Identifikationsnummer", "Zeichnernummer", "Postensperre bis",
    "Bezeichnung SoBil-Sachverhalt", "Kennzeichen SoBil-Buchung", "Festschreibung", "Leistungsdatum",
    "Datum Zuord. Steuerperiode",
]
assert len(DATEV_COLUMNS) == 116, f"DATEV_COLUMNS must have 116 columns (format version 12), has {len(DATEV_COLUMNS)}"


def _extf_header(period: str, year: int) -> list[str]:
    """The EXTF meta header line (one row, before the column header row)."""
    settings = get_settings()
    berater = str(getattr(settings, "datev_berater_nr", "") or "")
    mandant = str(getattr(settings, "datev_mandant_nr", "") or "")
    wj_beginn = str(getattr(settings, "datev_wj_beginn", "") or f"{year}0101")
    created = now().strftime("%Y%m%d%H%M%S") + "000"
    start_s, end_s = period.replace("-", "") + "01", period.replace("-", "") + "31"
    return ["EXTF", "700", "21", "Buchungsstapel", "12", created, "", "RE", "aecom", "", berater, mandant,
            wj_beginn, "4", start_s, end_s, f"Buchungsstapel {period}", "", "1", "0", "0", "EUR"]


def _money(amount: Decimal) -> str:
    return str(amount.quantize(Decimal("0.01"))).replace(".", ",").replace("-", "")


def _row(values: dict[int, str]) -> list[str]:
    out = [""] * len(DATEV_COLUMNS)
    for i, v in values.items():
        out[i] = v
    return out


def _datev_rows(txns: dict[str, list[LedgerEntry]], amap: dict[str, str], overridden: set[str], s: Session,
                warnings: list[str]) -> list[list[str]]:
    idx = {name: i for i, name in enumerate(DATEV_COLUMNS)}
    defaults_used: set[str] = set()
    rows: list[list[str]] = []
    for txn_id, lines in sorted(txns.items()):
        memo = lines[0].memo
        ref = lines[0].ref
        receipt = _receipt_for(s, ref)
        if any(e.account in EXPENSE_ACCOUNTS for e in lines) and not ref:
            warnings.append(f"missing receipt: txn {txn_id} ({memo})")
        belegdatum = min(e.created_at for e in lines).strftime("%d%m")
        for d_entry, c_entry, amount in _split_txn(lines):
            country = next((e.country for e in (d_entry, c_entry) if e.country), None)
            rate = next((e.vat_rate for e in (d_entry, c_entry) if e.vat_rate is not None), None)
            d_acct, d_default = _konto(d_entry.account, country, rate, amap, overridden)
            c_acct, c_default = _konto(c_entry.account, country, rate, amap, overridden)
            if d_default:
                defaults_used.add(d_entry.account)
            if c_default:
                defaults_used.add(c_entry.account)
            if d_entry.account == "cash":
                konto, gegenkonto, sh = d_acct, c_acct, "S"
            elif c_entry.account == "cash":
                konto, gegenkonto, sh = c_acct, d_acct, "H"
            else:
                konto, gegenkonto, sh = d_acct, c_acct, "S"
            rows.append(_row({
                idx["Umsatz (ohne Soll/Haben-Kz)"]: _money(amount),
                idx["Soll/Haben-Kennzeichen"]: sh,
                idx["WKZ Umsatz"]: "EUR",
                idx["Konto"]: konto,
                idx["Gegenkonto (ohne BU-Schluessel)"]: gegenkonto,
                idx["BU-Schluessel"]: "",  # VAT is booked as its own manual account line, not DATEV automatic tax
                idx["Belegdatum"]: belegdatum,
                idx["Belegfeld 1"]: (receipt or ref or "")[:36],
                idx["Buchungstext"]: memo[:60],
            }))
    if defaults_used:
        warnings.append("default SKR03 account numbers used for: " + ", ".join(sorted(defaults_used)) +
                        " - confirm with the accountant or override via account_map / accounting_account_map")
    return rows


def _write_datev(period: str, year: int, rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(_extf_header(period, year))
    w.writerow(DATEV_COLUMNS)
    w.writerows(rows)
    return buf.getvalue().encode("cp1252", errors="replace")


def _write_csv(rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "txn", "account", "debit", "credit", "memo", "ref", "country", "vat_rate", "receipt"])
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def export_month(period: str, fmt: str = "csv", *, by: str, account_map: dict[str, str] | None = None) -> ExportResult:
    """General journal for one month (``period='YYYY-MM'``), as ``'datev'`` (Buchungsstapel) or ``'csv'``.

    Refuses a month that has not ended yet; a month that is not closed in ``ops_periods`` only adds a warning.
    Every call is audited (``accountant_export``, ``human:<by>``); the audit detail never carries customer emails
    (ledger refs are order/campaign/vendor ids, not emails) or ``.env`` values.
    """
    if fmt not in FORMATS:
        raise ValueError(f"fmt must be one of {FORMATS}, got {fmt!r}")
    if not by or not by.strip():
        raise ValueError("by (who requested the export) is required for the audit trail")
    start, end, year, _month = _period_bounds(period)
    warnings = _period_warnings(period, start, end)
    amap, overridden = _account_map(account_map)

    with session() as s:
        txns = _txns_in_period(s, start, end)
        if fmt == "datev":
            data_rows = _datev_rows(txns, amap, overridden, s, warnings)
            warnings.insert(0, DATEV_DISCLAIMER)
            content = _write_datev(period, year, data_rows)
            filename, encoding = f"datev_buchungsstapel_{period}.csv", "cp1252"
        else:
            csv_rows: list[list[Any]] = []
            for txn_id, lines in sorted(txns.items()):
                for e in lines:
                    receipt = _receipt_for(s, e.ref)
                    if e.account in EXPENSE_ACCOUNTS and not e.ref:
                        warnings.append(f"missing receipt: txn {txn_id} ({e.memo})")
                    csv_rows.append([e.created_at.strftime("%Y-%m-%d"), txn_id, e.account, f"{e.debit:.2f}",
                                     f"{e.credit:.2f}", e.memo, e.ref or "", e.country or "",
                                     e.vat_rate if e.vat_rate is not None else "", receipt or ""])
            content = _write_csv(csv_rows)
            filename, encoding = f"general_journal_{period}.csv", "utf-8"

    rows = len(data_rows) if fmt == "datev" else len(csv_rows)
    audit(f"human:{by}", "accountant_export", "info",
          {"period": period, "fmt": fmt, "rows": rows, "warnings": len(warnings)})
    return ExportResult(filename=filename, content=content, rows=rows, warnings=warnings, encoding=encoding)
