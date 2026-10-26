"""I6/I7: refunds reverse VAT and may be partial; the licence key goes only with the last cent."""

import pytest
from sqlalchemy import func, select

from aecom import ledger
from aecom.db import Customer, LedgerEntry, LicenseKey, Refund, session
from aecom.money import cents, vat_split
from aecom.store.adapter import get_store

SKU = "LIC-REFUND-V1"


@pytest.fixture
def store():
    st = get_store()
    st.create_product(SKU, "Refund test licence", 49.0, "license")
    st.add_license_keys(SKU, [f"KEY-{i:04d}" for i in range(10)])
    st.set_status(SKU, "active")
    return st


def _sell(st, email: str, country: str) -> str:
    oid = st.create_order(email, SKU, country=country)["id"]
    st.deliver_order(oid)
    return oid


def _order_totals(order_id: str) -> dict[str, int]:
    """Signed balance per account (in cents) of all ledger rows referencing one order."""
    out: dict[str, int] = {}
    with session() as s:
        for e in s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order_id)):
            sign = 1 if e.account in ledger.DEBIT_NORMAL else -1
            out[e.account] = out.get(e.account, 0) + sign * (cents(e.debit) - cents(e.credit))
    return out


def _assert_balanced() -> None:
    with session() as s:
        d, c = s.execute(select(func.sum(LedgerEntry.debit), func.sum(LedgerEntry.credit))).one()
    assert cents(d or 0) == cents(c or 0)


def _key(order_id: str) -> str:
    with session() as s:
        return s.scalars(select(LicenseKey.status).where(LicenseKey.order_id == order_id)).one()


def test_full_refund_reverses_vat_exactly(store):
    oid = _sell(store, "full@example.com", "DE")
    assert _order_totals(oid)["vat_payable"] == cents(vat_split(49.0, 0.19)[1])
    res = store.refund_order(oid, 49.0, "changed mind")
    assert res["status"] == "refunded" and _key(oid) == "revoked"
    t = _order_totals(oid)
    assert t["vat_payable"] == 0 and t["revenue"] - t["refunds"] == 0 and t["cash"] == 0
    with session() as s:
        line = s.scalars(select(LedgerEntry).where(LedgerEntry.ref == oid, LedgerEntry.memo.startswith("refund"),
                                                   LedgerEntry.account == "vat_payable")).one()
        assert (line.country, line.vat_rate) == ("DE", 0.19)
    _assert_balanced()
    assert store.refund_order(oid, 49.0, "again") == {"order_id": oid, "status": "refunded", "already": True}
    _assert_balanced()


def test_partial_refunds_then_completion(store):
    oid = _sell(store, "part@example.com", "FR")
    sale_vat = cents(vat_split(49.0, 0.20)[1])
    res = store.refund_order(oid, 20.0, "partial goodwill")
    assert res["status"] == "partially_refunded" and res["remaining_eur"] == 29.0
    assert store.get_order(oid)["status"] == "partially_refunded" and _key(oid) == "assigned"
    t = _order_totals(oid)
    assert t["vat_payable"] == sale_vat - cents(vat_split(20.0, 0.20)[1])  # 8.17 - 3.33
    assert t["refunds"] == cents(vat_split(20.0, 0.20)[0])
    _assert_balanced()

    res = store.refund_order(oid, 29.0, "rest")
    assert res["status"] == "refunded" and _key(oid) == "revoked"
    t = _order_totals(oid)
    assert t["vat_payable"] == 0 and t["revenue"] == t["refunds"]  # cumulative split: no stray cent
    _assert_balanced()
    assert store.refund_order(oid, 1.0, "more")["already"] is True


def test_over_refund_raises_and_writes_nothing(store):
    oid = _sell(store, "over@example.com", "DE")
    store.refund_order(oid, 40.0, "partial")
    with session() as s:
        before = (s.scalar(select(func.count(LedgerEntry.id))), s.scalar(select(func.count(Refund.id))))
    for bad in (9.01, 0, -1.0):
        with pytest.raises(ValueError, match="refund exceeds remaining amount"):
            store.refund_order(oid, bad, "too much")
    with session() as s:
        after = (s.scalar(select(func.count(LedgerEntry.id))), s.scalar(select(func.count(Refund.id))))
    assert after == before and store.get_order(oid)["status"] == "partially_refunded" and _key(oid) == "assigned"
    _assert_balanced()


def test_multiple_countries_and_pnl(store):
    before = ledger.pnl()
    orders = {c: _sell(store, f"{c.lower()}@example.com", c) for c in ("DE", "FR", "HU", "US")}
    for c, oid in orders.items():
        store.refund_order(oid, 49.0 if c in ("DE", "US") else 10.0, "mixed")
        _assert_balanced()
    p = ledger.pnl()
    remaining_vat = sum(cents(vat_split(49.0, r)[1]) - cents(vat_split(10.0, r)[1]) for r in (0.20, 0.27))
    assert cents(p["vat_payable"]) - cents(before["vat_payable"]) == remaining_vat
    remaining_net = sum(cents(vat_split(49.0, r)[0]) - cents(vat_split(10.0, r)[0]) for r in (0.20, 0.27))
    assert cents(p["net_revenue"]) - cents(before["net_revenue"]) == remaining_net
    assert all(_order_totals(orders[c])["vat_payable"] == 0 for c in ("DE", "US"))


def test_customer_country_stored_uppercase(store):
    oid = _sell(store, "lower@example.com", "fr")
    with session() as s:
        assert s.scalars(select(Customer.country).where(Customer.email == "lower@example.com")).one() == "FR"
    store.refund_order(oid, 49.0, "full")
    assert _order_totals(oid)["vat_payable"] == 0


def test_two_argument_record_refund_uses_sale_rate(store):
    oid = _sell(store, "direct@example.com", "IT")
    ledger.record_refund(oid, 49.0)
    t = _order_totals(oid)
    assert t["vat_payable"] == 0 and t["revenue"] == t["refunds"]
    _assert_balanced()
