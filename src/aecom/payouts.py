"""F4: payout reconciliation - platform payouts and fees into the ledger.

Platforms (Lemon Squeezy, Stripe, PayPal, ...) pay out to the bank net of fees, on their own schedule, often not
1:1 with individual orders. This module ingests one payout record per payout (from a CSV export, or optionally
the Stripe API), then reconciles it against the orders this app mirrored (``aecom.store.lemonsqueezy``) and the
refunds booked for them inside the payout's covered period::

    expected net = gross orders in period - refunds in period - platform fees

If that matches ``net_eur`` (what actually reached the bank) to the cent, the payout is ``matched``; otherwise
``mismatch``, with the signed ``difference_eur`` (expected minus actual, see ``db.Payout``) left for a human to
investigate. Either way, on the *first* reconciliation the platform's fees are booked once as a balanced ledger
entry (debit ``vendor_expense``, credit ``cash``, memo ``"platform fees <provider> <external_id>"``) so R13's
"book Lemon Squeezy fees as expenses" shows up in the P&L. This never double-books (``fee_txn`` on the row
remembers the ledger transaction) and refuses to post into a closed month: ``aecom.periods`` raises
``PeriodClosed`` (a ``ValueError`` with a clear message), which propagates here and leaves the payout - and
everything else about it - exactly as it was; nothing partial is ever committed.

    payouts.ingest(provider, record, by)             # one payout, idempotent on (provider, external_id)
    payouts.import_csv(provider, text, mapping, by)   # a payout export (any platform's CSV), flexible columns
    payouts.reconcile(payout_id, by)                  # match it and (once) book its fees
    payouts.list_payouts(status=None)
    payouts.unmatched()                               # pending or mismatch - the dashboard and the monitor read this
    payouts.fetch_stripe(since, by)                   # optional: Stripe API, via net_guard (see its own
                                                       # docstring - the live call itself is untested, see
                                                       # docs/wiki/Payouts.md)

Covered orders today are Lemon Squeezy mirrors only (``Order.external_id`` set) - the only storefront this app
integrates with (``aecom.store.lemonsqueezy``). A payout ingested for another provider still reconciles, it just
finds no local orders to match against (a mismatch, unless its fees also happen to be zero) until that provider's
orders are mirrored here too.
"""

from __future__ import annotations

import csv
import io
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from aecom import ledger
from aecom.audit import audit
from aecom.db import Order, Payout, Refund, session
from aecom.money import cents, quantize
from aecom.net_guard import pinned_get, resolve_public
from aecom.secrets import secrets

LEMONSQUEEZY = "lemonsqueezy"
STRIPE = "stripe"
PAYPAL = "paypal"
PROVIDERS = (LEMONSQUEEZY, STRIPE, PAYPAL)
MIRRORED_PROVIDERS = (LEMONSQUEEZY,)  # providers whose orders this app actually mirrors locally (see module doc)

STALE_AFTER_DAYS = 14  # aecom.monitors.payout_unmatched: pending/mismatch older than this is a P3

MONEY_FIELDS = ("gross_eur", "fees_eur", "refunds_eur", "net_eur")
DATE_FIELDS = ("period_start", "period_end")
REQUIRED_FIELDS = ("external_id", "gross_eur", "fees_eur", "net_eur")
# Common export headers across Lemon Squeezy, Stripe and PayPal payout/transaction CSVs, lower-cased.
HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "external_id": ("payout id", "payout_id", "id", "external_id", "reference", "payout reference", "transaction id"),
    "period_start": ("period start", "period_start", "start date", "start_date"),
    "period_end": ("period end", "period_end", "end date", "end_date"),
    "currency": ("currency",),
    "gross_eur": ("gross", "gross amount", "gross_eur", "amount", "total"),
    "fees_eur": ("fees", "fee", "fee amount", "fees_eur"),
    "refunds_eur": ("refunds", "refund amount", "refunds_eur", "refunded"),
    "net_eur": ("net", "net amount", "net_eur", "amount paid out", "payout amount", "net total"),
    "note": ("note", "notes", "description"),
}
ALL_FIELDS = tuple(HEADER_ALIASES)


def _actor(by: str) -> str:
    return f"human:{by}" if (by or "").strip() else "system"


def _row(p: Payout) -> dict[str, Any]:
    return {
        "id": p.id, "provider": p.provider, "external_id": p.external_id,
        "period_start": p.period_start.isoformat() if p.period_start else None,
        "period_end": p.period_end.isoformat() if p.period_end else None,
        "currency": p.currency, "gross_eur": p.gross_eur, "fees_eur": p.fees_eur,
        "refunds_eur": p.refunds_eur, "net_eur": p.net_eur, "status": p.status,
        "difference_eur": p.difference_eur, "matched_orders": p.matched_orders,
        "fee_txn": p.fee_txn, "note": p.note,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _parse_amount(value: Any, what: str) -> float:
    """A money figure from a CSV cell or an API response: accepts plain numbers, ``1,234.56``, ``€1234.56`` and
    ``(12.00)`` (accounting negative). Raises ``ValueError`` naming ``what`` (the caller passes the row/column)."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for junk in ("EUR", "eur", "\u20ac", "$", "USD", "usd", ","):
        text = text.replace(junk, "")
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{what}: {value!r} is not a number") from None


_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y")


def _parse_dt(value: Any, what: str) -> datetime | None:
    """A date/time from a CSV cell or API value: ISO, common export formats, or already a date/datetime. Raises
    ``ValueError`` naming ``what`` if it is present but unrecognisable."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        raise ValueError(f"{what}: {value!r} is not a recognisable date") from None


# --------------------------------------------------------------------------- ingest
def ingest(provider: str, record: dict[str, Any], by: str) -> dict[str, Any]:
    """One payout record, idempotent on ``(provider, external_id)``: ingesting the same one again returns the
    existing row unchanged (``already: True``) rather than overwriting it - re-import a corrected export only
    before the payout has reconciled, or have a human fix the stored numbers directly.

    ``record`` carries ``external_id`` (required) and any of ``period_start``/``period_end`` (date or ISO
    string), ``currency`` (default ``EUR``), ``gross_eur``/``fees_eur``/``refunds_eur``/``net_eur`` (numbers or
    CSV-shaped strings, default 0) and ``note``.
    """
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}, got {provider!r}")
    external_id = str(record.get("external_id") or "").strip()
    if not external_id:
        raise ValueError("external_id is required")
    with session() as s:
        existing = s.scalars(select(Payout).where(Payout.provider == provider,
                                                   Payout.external_id == external_id)).first()
        if existing is not None:
            row = _row(existing)
            audit(_actor(by), "payout_ingest", "duplicate", {"provider": provider, "external_id": external_id})
            return {**row, "already": True}
        payout = Payout(
            provider=provider, external_id=external_id,
            period_start=_parse_dt(record.get("period_start"), "period_start"),
            period_end=_parse_dt(record.get("period_end"), "period_end"),
            currency=str(record.get("currency") or "EUR").upper(),
            gross_eur=float(quantize(_parse_amount(record.get("gross_eur"), "gross_eur"))),
            fees_eur=float(quantize(_parse_amount(record.get("fees_eur"), "fees_eur"))),
            refunds_eur=float(quantize(_parse_amount(record.get("refunds_eur"), "refunds_eur"))),
            net_eur=float(quantize(_parse_amount(record.get("net_eur"), "net_eur"))),
            note=str(record.get("note") or ""),
        )
        s.add(payout)
        s.flush()
        row = _row(payout)
    audit(_actor(by), "payout_ingest", "executed",
          {"provider": provider, "external_id": external_id, "payout_id": row["id"]})
    return {**row, "already": False}


def _resolve_columns(fieldnames: list[str], mapping: dict[str, str] | None) -> dict[str, str]:
    """``{canonical field: actual CSV column name}``, from an explicit ``mapping`` first, then the common header
    aliases (``HEADER_ALIASES``). Raises ``ValueError`` naming the missing field or the bad mapping entry."""
    if not fieldnames:
        raise ValueError("the CSV has no header row")
    lookup = {h.strip().lower(): h for h in fieldnames}
    mapping = mapping or {}
    resolved: dict[str, str] = {}
    for field, col in mapping.items():
        if field not in ALL_FIELDS:
            raise ValueError(f"mapping names an unknown field {field!r}; expected one of {ALL_FIELDS}")
        if col not in fieldnames:
            raise ValueError(f"mapping names column {col!r} for {field!r}, but the CSV header has no such "
                             f"column (header: {', '.join(fieldnames)})")
        resolved[field] = col
    for field in ALL_FIELDS:
        if field in resolved:
            continue
        for alias in HEADER_ALIASES[field]:
            if alias in lookup:
                resolved[field] = lookup[alias]
                break
    missing = [f for f in REQUIRED_FIELDS if f not in resolved]
    if missing:
        raise ValueError(f"could not find a column for required field(s) {', '.join(missing)} in the CSV header "
                         f"({', '.join(fieldnames)}); pass mapping={{'<field>': '<your column name>'}} to name it")
    return resolved


def import_csv(provider: str, text: str, mapping: dict[str, str] | None = None, by: str = "") -> dict[str, Any]:
    """Import a payout export (Lemon Squeezy, Stripe, PayPal, ... any platform's CSV) with flexible column
    mapping: common headers are recognised automatically (``HEADER_ALIASES``); pass ``mapping`` to name a column
    explicitly when a platform's export uses something else, e.g. ``mapping={"external_id": "Payout Reference"}``.
    Every parsed row is ingested through ``ingest`` (idempotent, so re-importing the same file is safe).

    Errors are plain and name the row (1-based; the header is row 1, so the first data row is row 2) and column,
    e.g. ``"row 3, column 'Gross': 'n/a' is not a number"``, so a human can go straight to the bad cell.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    columns = _resolve_columns(reader.fieldnames or [], mapping)
    results: list[dict[str, Any]] = []
    for row_num, row in enumerate(reader, start=2):
        record: dict[str, Any] = {}
        for field, col in columns.items():
            value = row.get(col)
            if field in MONEY_FIELDS:
                record[field] = _parse_amount(value, f"row {row_num}, column {col!r}")
            elif field in DATE_FIELDS:
                record[field] = _parse_dt(value, f"row {row_num}, column {col!r}")
            else:
                record[field] = value
        if not str(record.get("external_id") or "").strip():
            raise ValueError(f"row {row_num}, column {columns['external_id']!r}: external id is required")
        results.append(ingest(provider, record, by))
    return {"count": len(results), "imported": sum(1 for r in results if not r["already"]),
            "duplicates": sum(1 for r in results if r["already"]), "payouts": results}


# --------------------------------------------------------------------------- reconcile
def _covered_orders(s: Any, payout: Payout) -> tuple[list[Order], list[Refund]]:
    """Mirrored orders paid, and refunds booked, inside the payout's covered period (see module docstring for why
    only Lemon Squeezy orders match today). Without both period bounds, or for an unmirrored provider, nothing is
    covered - the payout simply mismatches against its own fees until it can be matched for real."""
    if payout.provider not in MIRRORED_PROVIDERS or payout.period_start is None or payout.period_end is None:
        return [], []
    start, end = payout.period_start, payout.period_end
    orders = list(s.scalars(select(Order).where(
        Order.external_id.is_not(None), Order.created_at >= start, Order.created_at < end).order_by(Order.id)))
    mirrored_order_ids = select(Order.id).where(Order.external_id.is_not(None))
    refunds = list(s.scalars(select(Refund).where(
        Refund.created_at >= start, Refund.created_at < end, Refund.order_id.in_(mirrored_order_ids))
        .order_by(Refund.id)))
    return orders, refunds


def reconcile(payout_id: str, by: str = "") -> dict[str, Any]:
    """Match a payout against its covered orders and refunds, and (the first time only) book its fees as one
    balanced ledger entry. Safe to call again later (e.g. once more orders have been mirrored): it recomputes the
    match every time but only ever books the fees once (``fee_txn``).

    Refuses to post into a closed month: ``aecom.periods.PeriodClosed`` (a ``ValueError``) propagates with its own
    clear message, and - because the fee booking happens before anything on the payout row is changed, inside the
    same transaction - nothing is left half-done: the payout stays exactly as it was.
    """
    newly_booked = False
    with session() as s:
        payout = s.get(Payout, payout_id)
        if payout is None:
            raise ValueError(f"no payout {payout_id!r}")
        orders, refunds = _covered_orders(s, payout)
        gross_c = sum(cents(o.amount_eur) for o in orders)
        refunds_c = sum(cents(r.amount_eur) for r in refunds)
        fees_c = cents(payout.fees_eur)
        net_c = cents(payout.net_eur)
        diff_c = (gross_c - fees_c - refunds_c) - net_c
        status = "matched" if diff_c == 0 else "mismatch"
        if payout.fee_txn is None and fees_c:
            payout.fee_txn = ledger.post(
                [("vendor_expense", payout.fees_eur, 0), ("cash", 0, payout.fees_eur)],
                f"platform fees {payout.provider} {payout.external_id}", payout.id, session=s)
            newly_booked = True
        payout.status = status
        payout.difference_eur = diff_c / 100
        payout.matched_orders = sorted({o.id for o in orders} | {r.order_id for r in refunds})
        result = _row(payout)
    audit(_actor(by), "payout_reconcile", "executed",
          {"payout_id": payout_id, "status": result["status"], "difference_eur": result["difference_eur"],
           "fees_booked": newly_booked})
    return result


def list_payouts(status: str | None = None) -> list[dict[str, Any]]:
    with session() as s:
        q = select(Payout).order_by(Payout.created_at.desc())
        if status:
            q = q.where(Payout.status == status)
        return [_row(p) for p in s.scalars(q)]


def unmatched() -> list[dict[str, Any]]:
    """Payouts still needing attention: ``pending`` (not reconciled yet) or ``mismatch`` (reconciled, but the
    numbers don't add up) - what the dashboard and the ``payout_unmatched`` monitor both read."""
    with session() as s:
        rows = s.scalars(select(Payout).where(Payout.status.in_(("pending", "mismatch")))
                         .order_by(Payout.created_at)).all()
        return [_row(p) for p in rows]


# --------------------------------------------------------------------------- Stripe (optional)
STRIPE_BASE = "https://api.stripe.com/v1"
STRIPE_TIMEOUT_S = 15


def _stripe_get(path: str, key: str, **params: Any) -> dict[str, Any]:
    """One IP-pinned GET through ``net_guard`` (see its module docstring) - never plain ``httpx``, same pattern as
    ``aecom.integrations.ping``. LIVE CALL UNTESTED (see the module docstring and ``docs/wiki/Payouts.md``): tests
    monkeypatch ``resolve_public``/``pinned_get`` with recorded fixture JSON under ``tests/fixtures/stripe/``."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{STRIPE_BASE}{path}" + (f"?{query}" if query else "")
    target = resolve_public(url)
    response = pinned_get(target, timeout=STRIPE_TIMEOUT_S, headers={"Authorization": f"Bearer {key}"})
    response.raise_for_status()
    return response.json()


def _stripe_record(payout: dict[str, Any], transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """One Stripe payout object plus the balance transactions it paid out (``GET /v1/balance_transactions
    ?payout=<id>`` - Stripe's own documented reconciliation pattern, https://docs.stripe.com/payouts/reconciliation)
    turned into an ``ingest`` record. Only EUR payouts are supported (not converted; ingest others manually)."""
    currency = str(payout.get("currency") or "").upper()
    if currency != "EUR":
        raise ValueError(f"Stripe payout {payout.get('id')} is in {currency or '?'}, not EUR; not converted, "
                         "ingest it manually")
    gross_c = sum(t["amount"] for t in transactions if t.get("type") == "charge")
    refund_c = sum(-t["amount"] for t in transactions if t.get("type") == "refund")
    fee_c = sum(t.get("fee") or 0 for t in transactions)
    created = [t["created"] for t in transactions if t.get("created") is not None] or [payout.get("created")]
    start = datetime.fromtimestamp(min(created), tz=UTC).replace(tzinfo=None)
    end = datetime.fromtimestamp(max(created), tz=UTC).replace(tzinfo=None) + timedelta(seconds=1)
    return {
        "external_id": str(payout["id"]), "period_start": start, "period_end": end, "currency": "EUR",
        "gross_eur": gross_c / 100, "fees_eur": fee_c / 100, "refunds_eur": refund_c / 100,
        "net_eur": payout["amount"] / 100,
        "note": f"Stripe payout {payout['id']}, arrival {payout.get('arrival_date')}",
    }


def fetch_stripe(since: datetime | date | str, by: str = "system") -> dict[str, Any]:
    """Fetch paid Stripe payouts created on/after ``since`` and ingest each one (``STRIPE_API_KEY`` from
    ``aecom.secrets``). Optional: raises ``RuntimeError`` if the key is not set. LIVE CALL UNTESTED - see the
    module docstring and ``docs/wiki/Payouts.md`` (only the JSON parsing is covered, against a recorded fixture)."""
    key = secrets.get("STRIPE_API_KEY")
    if not key:
        raise RuntimeError("STRIPE_API_KEY is not set. A human provisions it and puts it in .env.")
    since_dt = since if isinstance(since, datetime) else _parse_dt(since, "since")
    params: dict[str, Any] = {"limit": 100, "status": "paid"}
    if since_dt:
        params["created[gte]"] = int(since_dt.replace(tzinfo=UTC).timestamp())
    body = _stripe_get("/payouts", key, **params)
    results = []
    for payout in body.get("data", []):
        txns = _stripe_get("/balance_transactions", key, payout=payout["id"], limit=100).get("data", [])
        results.append(ingest(STRIPE, _stripe_record(payout, txns), by))
    return {"fetched": len(results), "payouts": results}
