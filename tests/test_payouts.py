"""F4: payout reconciliation (``aecom.payouts``) - ingest idempotency, CSV import (common headers, mapping,
plain row/column errors), matched and mismatch reconciliation, fees booked exactly once and balanced, refusal to
post fees into a closed month, the month-end numbers (also covered in ``test_month_end_report.py``), the
``payout_unmatched`` P3 monitor, the optional Stripe fetch (parsing only, against a recorded fixture - the live
call itself is untested, see ``tests/fixtures/stripe/README.md``) and the dashboard tab.

Every test gets its own temp SQLite DB from ``tests/conftest.py``'s ``fresh_company`` fixture (autouse).
Reconciliation only matches *mirrored* orders (``Order.external_id`` set - a real Lemon Squeezy order, see the
``aecom.payouts`` module docstring): orders created through the default (mock) store adapter have their
``external_id`` set by hand here, the same technique ``tests/test_revenue_milestone.py`` uses to simulate a real
order.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from streamlit.testing.v1 import AppTest

from aecom import incidents, monitors, payouts, periods
from aecom.db import LedgerEntry, Order, Payout, Period, Refund, now, session
from aecom.simulator import _backdate
from aecom.store.adapter import get_store

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
sys.path.insert(0, str(DASHBOARD))  # the partial does `from _common import ...`

FIX = Path(__file__).parent / "fixtures" / "stripe"

# A period wide enough to contain any order this test file creates "now", regardless of when it runs.
WIDE_START = datetime(2000, 1, 1)
WIDE_END = datetime(2100, 1, 1)


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


@pytest.fixture
def store():
    s = get_store()
    s.create_product("PAY-TEST", "Payout test product", 100.0, "download", unit_cost_eur=10.0)
    s.set_status("PAY-TEST", "active")
    return s


def _mirrored_order(store, email: str, ts: datetime | None = None) -> str:
    """One order, marked as a real (mirrored) store-platform order by giving it an ``external_id`` - the same
    technique ``tests/test_revenue_milestone.py`` uses. With ``ts``, backdated into that period (``_backdate``,
    the same helper ``aecom.simulator`` and ``test_month_end_report.py`` use)."""
    oid = store.create_order(email, "PAY-TEST", country="DE")["id"]
    if ts is not None:
        _backdate(oid, ts)
    with session() as s:
        s.get(Order, oid).external_id = f"ls-{oid}"
    return oid


def _record(**over) -> dict:
    base = {"external_id": "po_1", "period_start": WIDE_START, "period_end": WIDE_END, "currency": "EUR",
           "gross_eur": 0.0, "fees_eur": 0.0, "refunds_eur": 0.0, "net_eur": 0.0}
    return {**base, **over}


# --------------------------------------------------------------------------- ingest
def test_ingest_is_idempotent_on_provider_and_external_id():
    rec = _record(external_id="dup1", gross_eur=10.0, fees_eur=1.0, net_eur=9.0)
    first = payouts.ingest(payouts.LEMONSQUEEZY, rec, "Ada")
    assert first["already"] is False and first["status"] == "pending"
    again = payouts.ingest(payouts.LEMONSQUEEZY, {**rec, "gross_eur": 999.0}, "Ada")  # different numbers: ignored
    assert again["already"] is True and again["id"] == first["id"] and again["gross_eur"] == 10.0
    assert len(payouts.list_payouts()) == 1
    # the same external id under a different provider is a different payout
    other = payouts.ingest(payouts.STRIPE, rec, "Ada")
    assert other["already"] is False and len(payouts.list_payouts()) == 2


def test_ingest_validates_provider_and_external_id():
    with pytest.raises(ValueError, match="provider must be one of"):
        payouts.ingest("square", _record(), "Ada")
    with pytest.raises(ValueError, match="external_id is required"):
        payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id=""), "Ada")


def test_ingest_audits_as_the_human_or_system():
    payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id="aud1"), "Ada")
    payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id="aud2"), "")
    with session() as s:
        from aecom.db import AuditLog

        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "payout_ingest")))
    actors = {r.actor for r in rows}
    assert "human:Ada" in actors and "system" in actors


# --------------------------------------------------------------------------- import_csv
LS_CSV = ("Payout ID,Period Start,Period End,Currency,Gross,Fees,Refunds,Net\n"
         "ls_1,2026-01-01,2026-02-01,EUR,500.00,25.00,10.00,465.00\n")


def test_import_csv_recognises_common_headers():
    result = payouts.import_csv(payouts.LEMONSQUEEZY, LS_CSV, by="Ada")
    assert result == {"count": 1, "imported": 1, "duplicates": 0, "payouts": result["payouts"]}
    row = result["payouts"][0]
    assert row["external_id"] == "ls_1" and row["gross_eur"] == 500.0 and row["fees_eur"] == 25.0
    assert row["refunds_eur"] == 10.0 and row["net_eur"] == 465.0
    assert row["period_start"] == "2026-01-01T00:00:00" and row["period_end"] == "2026-02-01T00:00:00"


def test_import_csv_is_idempotent_across_a_reimport():
    payouts.import_csv(payouts.LEMONSQUEEZY, LS_CSV, by="Ada")
    second = payouts.import_csv(payouts.LEMONSQUEEZY, LS_CSV, by="Ada")
    assert second["imported"] == 0 and second["duplicates"] == 1
    assert len(payouts.list_payouts()) == 1


def test_import_csv_explicit_mapping_for_an_unusual_header():
    text = "Ref,Total,Charge,RefundAmt,Payout\nx1,100,5,0,95\n"
    mapping = {"external_id": "Ref", "gross_eur": "Total", "fees_eur": "Charge", "refunds_eur": "RefundAmt",
              "net_eur": "Payout"}
    result = payouts.import_csv(payouts.STRIPE, text, mapping=mapping, by="Ada")
    row = result["payouts"][0]
    assert result["imported"] == 1
    assert (row["external_id"], row["gross_eur"], row["fees_eur"], row["net_eur"]) == ("x1", 100.0, 5.0, 95.0)


def test_import_csv_mapping_rejects_a_column_not_in_the_header():
    text = "Ref,Total\nx1,100\n"
    with pytest.raises(ValueError, match="no such column"):
        payouts.import_csv(payouts.STRIPE, text, mapping={"external_id": "Reference"}, by="Ada")


def test_import_csv_error_names_the_row_and_column():
    bad = "Payout ID,Gross,Fees,Net\nls_2,not-a-number,1,2\n"
    with pytest.raises(ValueError, match=r"row 2, column 'Gross': 'not-a-number' is not a number"):
        payouts.import_csv(payouts.LEMONSQUEEZY, bad, by="Ada")
    assert payouts.list_payouts() == []  # the bad row imported nothing


def test_import_csv_error_names_a_missing_required_column():
    bad = "Payout ID,Fees,Net\nls_3,1,2\n"
    with pytest.raises(ValueError, match="required field.*gross_eur"):
        payouts.import_csv(payouts.LEMONSQUEEZY, bad, by="Ada")


def test_import_csv_error_when_external_id_cell_is_blank():
    bad = "Payout ID,Gross,Fees,Net\n,10,1,9\n"
    with pytest.raises(ValueError, match=r"row 2, column 'Payout ID': external id is required"):
        payouts.import_csv(payouts.LEMONSQUEEZY, bad, by="Ada")


def test_import_csv_has_no_header_row():
    with pytest.raises(ValueError, match="no header row"):
        payouts.import_csv(payouts.LEMONSQUEEZY, "", by="Ada")


# --------------------------------------------------------------------------- reconcile: matched / mismatch
def test_reconcile_matches_when_numbers_add_up(store):
    _mirrored_order(store, "a@example.com")  # gross 100
    _mirrored_order(store, "b@example.com")  # gross 100
    payout = payouts.ingest(payouts.LEMONSQUEEZY,
                            _record(external_id="ls_match", gross_eur=200.0, fees_eur=10.0, net_eur=190.0), "Ada")
    result = payouts.reconcile(payout["id"], "Ada")
    assert result["status"] == "matched" and result["difference_eur"] == 0.0
    assert sorted(result["matched_orders"]) == sorted(payouts.list_payouts()[0]["matched_orders"])
    assert result["fee_txn"] is not None


def test_reconcile_counts_refunds_inside_the_period(store):
    o1 = _mirrored_order(store, "a@example.com")
    get_store().refund_order(o1, 20.0, "too expensive")
    payout = payouts.ingest(payouts.LEMONSQUEEZY,
                            _record(external_id="ls_refund", gross_eur=100.0, fees_eur=5.0, refunds_eur=20.0,
                                    net_eur=75.0), "Ada")
    result = payouts.reconcile(payout["id"], "Ada")
    assert result["status"] == "matched"  # 100 - 5 - 20 == 75
    assert o1 in result["matched_orders"]


def test_reconcile_flags_a_mismatch_with_signed_difference(store):
    _mirrored_order(store, "a@example.com")  # gross 100
    payout = payouts.ingest(payouts.LEMONSQUEEZY,
                            _record(external_id="ls_mismatch", gross_eur=100.0, fees_eur=5.0, net_eur=90.0), "Ada")
    result = payouts.reconcile(payout["id"], "Ada")
    assert result["status"] == "mismatch"
    assert result["difference_eur"] == 5.0  # expected 95 (100-5) minus actual 90


def test_reconcile_unmirrored_provider_never_matches_local_orders(store):
    _mirrored_order(store, "a@example.com")
    payout = payouts.ingest(payouts.PAYPAL,
                            _record(external_id="pp_1", gross_eur=100.0, fees_eur=0.0, net_eur=100.0), "Ada")
    result = payouts.reconcile(payout["id"], "Ada")
    assert result["status"] == "mismatch" and result["matched_orders"] == []  # no PayPal orders mirrored here


def test_reconcile_unknown_payout_raises():
    with pytest.raises(ValueError, match="no payout"):
        payouts.reconcile("pay_doesnotexist", "Ada")


# --------------------------------------------------------------------------- fees: booked once, balanced
def test_fees_booked_once_and_balanced(store):
    _mirrored_order(store, "a@example.com")
    _mirrored_order(store, "b@example.com")
    payout = payouts.ingest(payouts.LEMONSQUEEZY,
                            _record(external_id="ls_fees", gross_eur=200.0, fees_eur=7.5, net_eur=192.5), "Ada")
    first = payouts.reconcile(payout["id"], "Ada")
    assert first["status"] == "matched" and first["fee_txn"]
    with session() as s:
        lines = list(s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == first["fee_txn"])))
    assert {(l.account, l.debit, l.credit) for l in lines} == {("vendor_expense", 7.5, 0.0), ("cash", 0.0, 7.5)}
    assert sum(l.debit for l in lines) == sum(l.credit for l in lines)  # balanced
    assert all(l.ref == payout["id"] for l in lines)
    assert all(l.memo == f"platform fees lemonsqueezy {payout['external_id']}" for l in lines)

    second = payouts.reconcile(payout["id"], "Ada")  # reconciling again must not book the fees a second time
    assert second["fee_txn"] == first["fee_txn"]
    with session() as s:
        count = s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.ref == payout["id"]))
    assert count == 2  # still just the one fee transaction's two lines


def test_zero_fees_never_book_a_ledger_entry(store):
    _mirrored_order(store, "a@example.com")
    payout = payouts.ingest(payouts.LEMONSQUEEZY,
                            _record(external_id="ls_nofee", gross_eur=100.0, fees_eur=0.0, net_eur=100.0), "Ada")
    result = payouts.reconcile(payout["id"], "Ada")
    assert result["status"] == "matched" and result["fee_txn"] is None
    with session() as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.ref == payout["id"])) == 0


# --------------------------------------------------------------------------- closed month refusal
def test_reconcile_refuses_posting_fees_into_a_closed_month_and_leaves_it_pending(store):
    _mirrored_order(store, "a@example.com")
    payout = payouts.ingest(payouts.LEMONSQUEEZY,
                            _record(external_id="ls_closed", gross_eur=100.0, fees_eur=5.0, net_eur=95.0), "Ada")
    this_month = f"{now().year}-{now().month:02d}"
    with session() as s:
        s.add(Period(period=this_month, status="closed", closed_by="Ada"))
    with pytest.raises(periods.PeriodClosed, match="is closed"):
        payouts.reconcile(payout["id"], "Ada")
    row = payouts.list_payouts()[0]
    assert row["status"] == "pending" and row["fee_txn"] is None and row["difference_eur"] == 0.0
    with session() as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.ref == payout["id"])) == 0


# --------------------------------------------------------------------------- list_payouts / unmatched
def test_list_payouts_filters_by_status_and_unmatched_covers_pending_and_mismatch(store):
    window_a = (datetime(2020, 1, 1), datetime(2020, 2, 1))
    window_b = (datetime(2021, 1, 1), datetime(2021, 2, 1))
    payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id="p_pending", net_eur=1.0), "Ada")
    _mirrored_order(store, "a@example.com", datetime(2020, 1, 15))
    mismatched = payouts.ingest(payouts.LEMONSQUEEZY, _record(
        external_id="p_mismatch", period_start=window_a[0], period_end=window_a[1], gross_eur=100.0,
        net_eur=50.0), "Ada")
    payouts.reconcile(mismatched["id"], "Ada")
    _mirrored_order(store, "b@example.com", datetime(2021, 1, 15))
    matched = payouts.ingest(payouts.LEMONSQUEEZY, _record(
        external_id="p_matched", period_start=window_b[0], period_end=window_b[1], gross_eur=100.0,
        net_eur=100.0), "Ada")
    payouts.reconcile(matched["id"], "Ada")

    assert {p["status"] for p in payouts.list_payouts()} == {"pending", "mismatch", "matched"}
    assert [p["external_id"] for p in payouts.list_payouts("matched")] == ["p_matched"]
    assert {p["external_id"] for p in payouts.unmatched()} == {"p_pending", "p_mismatch"}


# --------------------------------------------------------------------------- monitors: payout_unmatched (P3)
def test_payout_unmatched_is_registered_as_p3():
    assert monitors.MONITORS["payout_unmatched"][0] == "P3"


def test_payout_unmatched_raises_after_14_days_not_before_and_resolves():
    payout = payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id="stale1", net_eur=1.0), "Ada")
    assert monitors.payout_unmatched(now()) == []  # fresh, not yet stale
    with session() as s:
        s.get(Payout, payout["id"]).created_at = now() - timedelta(days=13, hours=23)
    assert monitors.payout_unmatched(now()) == []  # just under 14 days
    with session() as s:
        s.get(Payout, payout["id"]).created_at = now() - timedelta(days=14, minutes=5)
    problems = monitors.payout_unmatched(now())
    assert len(problems) == 1 and problems[0][0] == payout["id"]
    assert "14 days" in problems[0][1] and payout["id"] in problems[0][1]

    with session() as s:  # fixing it (matched) clears the problem
        s.get(Payout, payout["id"]).status = "matched"
    assert monitors.payout_unmatched(now()) == []


def test_payout_unmatched_via_run_monitors_opens_and_resolves_an_incident():
    payout = payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id="stale2", net_eur=1.0), "Ada")
    with session() as s:
        s.get(Payout, payout["id"]).created_at = now() - timedelta(days=15)
    monitors.run_monitors()
    active = {i["subject"]: i for i in incidents.list_incidents()
             if i["monitor"] == "payout_unmatched" and i["status"] in incidents.ACTIVE}
    assert payout["id"] in active and active[payout["id"]]["severity"] == "P3"

    with session() as s:
        s.get(Payout, payout["id"]).status = "matched"
    monitors.run_monitors()
    assert incidents.get_incident(active[payout["id"]]["id"])["status"] == "resolved"


# --------------------------------------------------------------------------- Stripe fetch (parsing only, offline)
class FakeStripeHTTP:
    """Records every `pinned_get` call; routes by path to the recorded fixture JSON. See
    `tests/fixtures/stripe/README.md` - the live call itself is untested, only this parsing is."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(payouts, "resolve_public", lambda url: url)  # identity: forwarded straight through
        monkeypatch.setattr(payouts, "pinned_get", self._pinned_get)

    def _pinned_get(self, target: str, *, timeout: float = 15, headers: dict | None = None) -> httpx.Response:
        self.calls.append(target)
        assert headers == {"Authorization": "Bearer sk_test_fixture"}
        if target.startswith(f"{payouts.STRIPE_BASE}/payouts"):
            body = fixture("payouts_list")
        else:
            body = fixture("balance_transactions_po_1PAYOUTfixture01")
        response = httpx.Response(200, json=body, request=httpx.Request("GET", target))
        return response


@pytest.fixture
def stripe_http(monkeypatch) -> FakeStripeHTTP:
    fake = FakeStripeHTTP()
    fake.install(monkeypatch)
    monkeypatch.setattr(payouts.secrets, "get", lambda name: "sk_test_fixture" if name == "STRIPE_API_KEY" else None)
    return fake


def test_fetch_stripe_ingests_the_fixture_payout(stripe_http):
    result = payouts.fetch_stripe(datetime(2026, 1, 1), by="Ada")
    assert result["fetched"] == 1
    row = result["payouts"][0]
    assert row["external_id"] == "po_1PAYOUTfixture01"
    # gross = 6000 + 4000, fees = 180 + 120, refunds = 500, net = payout.amount (9200) - see fixtures/stripe/README
    assert (row["gross_eur"], row["fees_eur"], row["refunds_eur"], row["net_eur"]) == (100.0, 3.0, 5.0, 92.0)
    assert stripe_http.calls[0].startswith(f"{payouts.STRIPE_BASE}/payouts?")
    assert "created%5Bgte%5D=" in stripe_http.calls[0] or "created[gte]=" in stripe_http.calls[0]


def test_fetch_stripe_requires_the_api_key(monkeypatch):
    monkeypatch.setattr(payouts.secrets, "get", lambda name: None)
    with pytest.raises(RuntimeError, match="STRIPE_API_KEY is not set"):
        payouts.fetch_stripe(datetime(2026, 1, 1), by="Ada")


def test_stripe_record_rejects_non_eur_currency():
    payout = {**fixture("payouts_list")["data"][0], "currency": "usd"}
    with pytest.raises(ValueError, match="not EUR"):
        payouts._stripe_record(payout, fixture("balance_transactions_po_1PAYOUTfixture01")["data"])


# --------------------------------------------------------------------------- dashboard tab
def _tab_named():
    from _acct_payouts import render_payouts_tab

    render_payouts_tab("Ada")


def _tab_blank():
    from _acct_payouts import render_payouts_tab

    render_payouts_tab("")


def _messages(at: AppTest) -> str:
    return " | ".join(str(e.value) for e in list(at.exception) + list(at.error))


def test_tab_shows_empty_state_with_no_payouts_yet():
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    assert any("No payouts yet" in m.value for m in at.markdown)


def test_tab_imports_a_csv_and_lists_it():
    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    uploader = at.file_uploader(key="_payout_import_file")
    uploader.set_value(("payout.csv", LS_CSV.encode("utf-8"), "text/csv"))
    at = at.run()
    assert not at.exception, _messages(at)
    at.button(key="_payout_import_btn").click().run()
    assert not at.exception, _messages(at)
    assert any("Imported 1 payout" in s.value for s in at.success)
    assert payouts.list_payouts()[0]["external_id"] == "ls_1"


def test_tab_reconcile_button_matches_a_payout(store):
    _mirrored_order(store, "a@example.com")
    payouts.ingest(payouts.LEMONSQUEEZY,
                   _record(external_id="ls_tab", gross_eur=100.0, fees_eur=0.0, net_eur=100.0), "Ada")

    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    reconcile_buttons = [b for b in at.button if str(b.key or "").startswith("_payout_reconcile_")
                        and b.key != "_payout_reconcile_all_btn"]
    assert len(reconcile_buttons) == 1
    reconcile_buttons[0].click().run()
    assert not at.exception, _messages(at)
    assert any("matched" in s.value for s in at.success)
    assert payouts.list_payouts()[0]["status"] == "matched"


def test_tab_blocks_reconciling_without_a_name():
    payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id="ls_noname", net_eur=1.0), "Ada")

    at = AppTest.from_function(_tab_blank).run()
    assert not at.exception, _messages(at)
    btn = [b for b in at.button if str(b.key or "").startswith("_payout_reconcile_")
          and b.key != "_payout_reconcile_all_btn"][0]
    btn.click().run()
    assert not at.exception, _messages(at)
    assert any("Enter your name" in w.value for w in at.warning)
    assert payouts.list_payouts()[0]["status"] == "pending"


def test_tab_reconcile_all_pending(store):
    _mirrored_order(store, "a@example.com")
    payouts.ingest(payouts.LEMONSQUEEZY,
                   _record(external_id="ls_all_1", gross_eur=100.0, fees_eur=0.0, net_eur=100.0), "Ada")
    payouts.ingest(payouts.LEMONSQUEEZY, _record(external_id="ls_all_2", net_eur=1.0), "Ada")  # stays mismatch

    at = AppTest.from_function(_tab_named).run()
    assert not at.exception, _messages(at)
    at.button(key="_payout_reconcile_all_btn").click().run()
    assert not at.exception, _messages(at)
    statuses = {p["external_id"]: p["status"] for p in payouts.list_payouts()}
    assert statuses["ls_all_1"] == "matched" and statuses["ls_all_2"] == "mismatch"


@pytest.mark.parametrize(("text", "value"), [
    ("123,45", 123.45), ("1,234.56", 1234.56), ("1.234,56", 1234.56), ("1.234.567,89", 1234567.89),
    ("(12,50)", -12.5), ("-0,99", -0.99), ("\u20ac 1 234,50", 1234.5), ("100", 100.0), ("12.5", 12.5),
    ("1,234,567.00", 1234567.0),
])
def test_amounts_in_us_and_european_formats_are_read_correctly(text, value):
    assert payouts._parse_amount(text, "row 1") == value


@pytest.mark.parametrize("text", ["1,234", "12,3456", "1.2,3.4", "abc"])
def test_ambiguous_or_broken_amounts_are_refused_not_guessed(text):
    with pytest.raises(ValueError, match="row 1"):
        payouts._parse_amount(text, "row 1")


def test_two_reconciles_at_the_same_moment_book_the_fees_once(store):
    import threading

    _mirrored_order(store, "a@example.com")
    payout = payouts.ingest(payouts.LEMONSQUEEZY,
                            _record(external_id="ls_race", gross_eur=100.0, fees_eur=4.0, net_eur=96.0), "Ada")
    errors, start = [], threading.Barrier(2)

    def go():
        try:
            start.wait()
            payouts.reconcile(payout["id"], "Ada")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    with session() as s:
        n = s.scalar(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.ref == payout["id"]))
    assert n == 2  # one debit and one credit line, not four
