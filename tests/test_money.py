from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aecom import ledger
from aecom.bootstrap import seed_demo
from aecom.db import Customer, LedgerEntry, Order, session
from aecom.money import cents, quantize, vat_split
from aecom.store.adapter import VAT_RATES, get_store, vat_included, vat_rate

EU27 = ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"]
SKU = "LIC-FOCUSFLOW-PRO-V1"


def test_quantize_and_cents_round_half_up():
    assert quantize(0.125) == Decimal("0.13")
    assert quantize(2.675) == Decimal("2.68")  # float 2.675 is 2.67499..., str() keeps the intended value
    assert cents(0.1 + 0.2) == 30
    assert cents("19.99") == 1999


@pytest.mark.parametrize("gross,country,net,vat", [
    (49.00, "FR", "40.83", "8.17"),
    (19.99, "DE", "16.80", "3.19"),
    (0.01, "DE", "0.01", "0.00"),
    (0.01, "HU", "0.01", "0.00"),
    (9.99, "FI", "7.96", "2.03"),
    (100, "US", "100.00", "0.00"),
])
def test_vat_split_rounding(gross, country, net, vat):
    assert vat_split(gross, VAT_RATES[country]) == (Decimal(net), Decimal(vat))
    assert vat_included(gross, country) == float(vat)


def test_net_plus_vat_equals_gross_everywhere():
    prices = [i / 100 for i in range(1, 2000, 7)] + [49.0, 99.99, 1234.56]
    for country, rate in VAT_RATES.items():
        for p in prices:
            net, vat = vat_split(p, rate)
            assert net + vat == quantize(p), (country, p)


def test_all_eu_member_states_have_2026_rates():
    assert set(EU27) <= set(VAT_RATES)
    assert len(EU27) == 27
    assert VAT_RATES["FI"] == 0.255 and VAT_RATES["EE"] == 0.24 and VAT_RATES["RO"] == 0.21 and VAT_RATES["SK"] == 0.23
    assert VAT_RATES["EL"] == VAT_RATES["GR"] and vat_rate("gb") == 0.20


def test_unknown_country_raises():
    with pytest.raises(ValueError, match="no VAT rate for country XX"):
        vat_included(10.0, "XX")


def test_ledger_rejects_one_cent_imbalance():
    with pytest.raises(ValueError):
        ledger.post([("cash", 10.00, 0), ("revenue", 0, 9.99)], "bad")


def test_ledger_accepts_float_noise_and_stores_cents():
    ledger.post([("cash", 0.1 + 0.2, 0), ("revenue", 0, 0.3)], "noise")
    ledger.record_expense("vendor_expense", 12.3456, "rounded")
    with session() as s:
        amounts = [e.debit + e.credit for e in s.scalars(select(LedgerEntry))]
    assert sorted(amounts) == [0.3, 0.3, 12.35, 12.35]


def test_unknown_country_creates_no_order_or_ledger_rows():
    seed_demo()
    with session() as s:
        before = [s.scalar(select(func.count()).select_from(t)) for t in (Order, Customer, LedgerEntry)]
    with pytest.raises(ValueError, match="no VAT rate"):
        get_store().create_order("x@example.com", SKU, "XX")
    with session() as s:
        assert [s.scalar(select(func.count()).select_from(t)) for t in (Order, Customer, LedgerEntry)] == before


def test_vat_payable_line_carries_country_and_rate():
    seed_demo()
    order = get_store().create_order("fi@example.com", SKU, "fi")
    with session() as s:
        lines = list(s.scalars(select(LedgerEntry).where(LedgerEntry.ref == order["id"])))
    vat_line = next(e for e in lines if e.account == "vat_payable")
    assert (vat_line.country, vat_line.vat_rate, vat_line.credit) == ("FI", 0.255, order["vat_eur"])
    assert all(e.country is None for e in lines if e.account != "vat_payable")
    assert sum(cents(e.debit) for e in lines) == sum(cents(e.credit) for e in lines)


def test_record_sale_defaults_stay_backward_compatible():
    txn = ledger.record_sale("o1", 49.0, 8.17, 7.0)
    with session() as s:
        vat_line = s.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == txn, LedgerEntry.account == "vat_payable")).one()
    assert vat_line.country is None and vat_line.vat_rate is None
    assert ledger.balances()["revenue"] == 40.83
