"""G2 webhooks: Lemon Squeezy webhook chaos (duplicate, out of order, lost) and lost-webhook recovery via
LemonSqueezyStore.reconcile_recent. Offline: fixtures from tests/fixtures/lemonsqueezy + an in-process API."""

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from aecom import db
from aecom.chaos import SCENARIOS, injectors, run_scenario
from aecom.chaos.invariants import every_mirrored_order_has_ledger_lines, refund_mirror_matches_provider
from aecom.db import Customer, ErrorRow, LedgerEntry, Order, Refund, session
from aecom.reconciler import reconcile
from aecom.store import lemonsqueezy as ls
from sqlalchemy import func, select

FIX = Path(__file__).parent / "fixtures" / "lemonsqueezy"
SINCE = datetime(2026, 10, 26)  # the fixture order was created 2026-10-27T10:00Z


def fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def api_order(name: str = "order_created", **attrs) -> dict:
    """The order object as GET /v1/orders lists it: no webhook meta, so no checkout custom data."""
    data = copy.deepcopy(fixture(name)["data"])
    data["attributes"].update(attrs)
    return data


@pytest.fixture
def store(fresh_company):
    fresh_company.lemonsqueezy_store_id = "7"
    return ls.LemonSqueezyStore()


def refunded_cents(external_id: str) -> int:
    with session() as s:
        o = s.scalars(select(Order).where(Order.external_id == external_id)).one()
        return round(s.scalar(select(func.coalesce(func.sum(Refund.amount_eur), 0.0))
                              .where(Refund.order_id == o.id)) * 100)


def counts() -> tuple[int, int, int]:
    with session() as s:
        return tuple(s.scalar(select(func.count()).select_from(m)) for m in (Order, Refund, LedgerEntry))


# ----------------------------------------------------------------------------- scenarios
@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("name", ["ls-duplicate", "ls-out-of-order", "ls-lost"])
def test_lemonsqueezy_webhook_scenarios_end_with_zero_violations(name, seed, fresh_company):
    r = run_scenario(name, seed)
    assert r["violations"] == []
    assert fresh_company.store_adapter == "mock"  # STORE_ADAPTER is restored afterwards
    with session() as s:
        mirrored = s.scalar(select(func.count(Order.id)).where(Order.external_id.is_not(None)))
    assert mirrored == r["stats"]["orders"] > 0


def test_scenarios_are_registered():
    assert {"ls-duplicate", "ls-out-of-order", "ls-lost"} <= set(SCENARIOS)


def test_every_webhook_is_delivered_twice_in_ls_duplicate():
    stats = run_scenario("ls-duplicate", 0)["stats"]
    assert stats["duplicates"] == stats["webhooks"] > stats["orders"]


def test_out_of_order_scenario_needs_provider_retries():
    stats = run_scenario("ls-out-of-order", 1)["stats"]
    assert stats["reordered"] > 0


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_lost_webhooks_are_recovered_by_the_reconciler(seed):
    stats = run_scenario("ls-lost", seed)["stats"]
    assert stats["lost"] > 0
    assert (stats["recovered"]["listed"], stats["recovered"]["errors"]) == (stats["orders"], 0)
    if seed:  # seed 0 only loses webhooks the mirror can do without (a duplicate refund, licence keys)
        assert stats["recovered"]["mirrored"] + stats["recovered"]["refunds"] > 0


def test_lost_webhooks_without_reconciler_leave_violations(monkeypatch):
    """The scenario would not be green without reconcile_recent: drop it and the mirror falls behind."""
    monkeypatch.setattr(ls.LemonSqueezyStore, "reconcile_recent", lambda self, since: {"mirrored": 0, "refunds": 0})
    found = {v["invariant"] for v in run_scenario("ls-lost", 1)["violations"]}
    assert "refund_mirror_matches_provider" in found


def test_out_of_order_puts_refunds_and_keys_before_orders():
    hooks = [{"meta": {"event_name": e}} for e in ("order_created", "license_key_created", "order_refunded")]
    with injectors.out_of_order(0):
        sent = injectors.ls_schedule(hooks)
    assert [h["meta"]["event_name"] for h in sent] == ["order_refunded", "license_key_created", "order_created"]
    assert injectors.ls_schedule(hooks) == hooks  # restored


# ----------------------------------------------------------------------------- invariants
def test_refund_mirror_matches_provider_reports_missing_and_mismatched(fresh_company):
    ls.mirror_order(fixture("order_created"))
    assert refund_mirror_matches_provider({"5001": 0}) == []
    assert [v["detail"] for v in refund_mirror_matches_provider({"5001": 1900, "5002": 0})] == [
        "provider order 5001: refunded 0 cents locally, 1900 at the provider", "provider order 5002 is not mirrored"]


def test_every_mirrored_order_has_ledger_lines_catches_a_double_booked_refund(fresh_company):
    ls.mirror_order(fixture("order_created"))
    ls.mirror_refund(fixture("order_refunded_partial"))
    assert every_mirrored_order_has_ledger_lines() == []
    with session() as s:
        o = s.scalars(select(Order).where(Order.external_id == "5001")).one()
        s.add(Refund(order_id=o.id, amount_eur=1.0, reason="booked twice"))
    assert len(every_mirrored_order_has_ledger_lines()) == 1


# ----------------------------------------------------------------------------- reconcile_recent
def test_reconcile_recent_mirrors_a_missing_order(store):
    with injectors.lemonsqueezy_api([api_order()], countries={"9001": "NL"}):
        r = store.reconcile_recent(SINCE)
    assert (r["listed"], r["mirrored"], r["refunds"], r["errors"]) == (1, 1, 0, 0)
    with session() as s:
        o = s.scalars(select(Order).where(Order.external_id == "5001")).one()
        assert (o.amount_eur, o.vat_eur, o.status) == (49.0, 8.5, "delivered")
        assert s.get(Customer, o.customer_id).country == "NL"  # looked up on the Lemon Squeezy customer
    assert every_mirrored_order_has_ledger_lines() == []


def test_reconcile_recent_corrects_a_refund_mismatch_and_is_idempotent(store):
    ls.mirror_order(fixture("order_created"))  # the order_refunded webhook was lost
    with injectors.lemonsqueezy_api([api_order("order_refunded_partial")]):
        first = store.reconcile_recent(SINCE)
        before = counts()
        second = store.reconcile_recent(SINCE)
    assert (first["refunds"], first["mirrored"]) == (1, 0)
    assert refunded_cents("5001") == 1900
    assert (second["refunds"], second["mirrored"], second["unchanged"]) == (0, 0, 1)
    assert counts() == before


def test_reconcile_recent_mirrors_a_missing_refunded_order(store):
    with injectors.lemonsqueezy_api([api_order("order_refunded_full")]):
        r = store.reconcile_recent(SINCE)
    assert (r["mirrored"], r["refunds"]) == (1, 1)
    assert refunded_cents("5001") == 4900
    with session() as s:
        assert s.scalars(select(Order.status).where(Order.external_id == "5001")).one() == "refunded"


def test_reconcile_recent_pages_until_older_than_since(store):
    orders = [api_order(created_at=f"2026-10-{d:02d}T10:00:00.000000Z") for d in (20, 25, 27, 28, 29)]
    for i, o in enumerate(orders):
        o["id"] = str(6000 + i)
    with injectors.lemonsqueezy_api(orders, page_size=2) as api:
        r = store.reconcile_recent(SINCE)
    assert (r["listed"], r["mirrored"]) == (3, 3)  # 27, 28, 29
    assert api["order_pages"] == 2  # page 2 reaches back past SINCE, page 3 is never asked for
    with session() as s:
        assert set(s.scalars(select(Order.external_id))) == {"6002", "6003", "6004"}


def test_reconcile_recent_records_a_failing_order_and_goes_on(store):
    bad = api_order(currency="USD")
    good = api_order(created_at="2026-10-27T09:00:00.000000Z")
    bad["id"], good["id"] = "6100", "6101"
    with injectors.lemonsqueezy_api([bad, good]):
        r = store.reconcile_recent(SINCE)
    assert (r["mirrored"], r["errors"]) == (1, 1)
    with session() as s:
        assert s.scalars(select(ErrorRow.component)).all() == ["lemonsqueezy.reconcile"]


# ----------------------------------------------------------------------------- reconciler
def test_reconciler_runs_reconcile_recent_for_the_lemonsqueezy_store(store, fresh_company, monkeypatch):
    seen = {}

    def fake(self, since):
        seen["since"] = since
        return {"listed": 1, "mirrored": 1, "refunds": 0, "unchanged": 0, "ignored": 0, "errors": 0}

    monkeypatch.setattr(ls.LemonSqueezyStore, "reconcile_recent", fake)
    assert "ls_mirrored" not in reconcile()  # mock store: no provider pass
    monkeypatch.setattr(fresh_company, "store_adapter", "lemonsqueezy")
    r = reconcile()
    assert (r["ls_listed"], r["ls_mirrored"]) == (1, 1)
    assert abs(db.now() - seen["since"] - timedelta(days=2)) < timedelta(minutes=1)


def test_reconciler_records_a_provider_failure_and_never_raises(store, fresh_company, monkeypatch):
    def down(self, since):
        raise RuntimeError("Lemon Squeezy API down")

    monkeypatch.setattr(ls.LemonSqueezyStore, "reconcile_recent", down)
    monkeypatch.setattr(fresh_company, "store_adapter", "lemonsqueezy")
    assert reconcile()["ls_errors"] == 1
    with session() as s:
        assert s.scalars(select(ErrorRow.component)).all() == ["reconciler.lemonsqueezy"]


# ----------------------------------------------------------------------------- marketing consent
@pytest.mark.parametrize("value", [True, "true"])
def test_marketing_consent_from_checkout_custom_data_is_recorded(fresh_company, value):
    payload = fixture("order_created")
    payload["meta"]["custom_data"]["marketing_consent"] = value
    ls.mirror_order(payload)
    with session() as s:
        c = s.scalars(select(Customer).where(Customer.email == "eva@example.nl")).one()
        assert (c.marketing_consent, c.marketing_consent_source) == (True, "checkout")
        assert c.marketing_consent_at == datetime(2026, 10, 27, 10, 0)


@pytest.mark.parametrize("value", [None, False, "false"])
def test_marketing_consent_absent_or_false_stays_false(fresh_company, value):
    payload = fixture("order_created")
    if value is not None:
        payload["meta"]["custom_data"]["marketing_consent"] = value
    ls.mirror_order(payload)
    with session() as s:
        c = s.scalars(select(Customer).where(Customer.email == "eva@example.nl")).one()
        assert (c.marketing_consent, c.marketing_consent_at, c.marketing_consent_source) == (False, None, None)


def test_reconcile_recovers_lost_licence_keys_through_the_fake_api(tmp_path):
    """ls-lost serves GET /license-keys, so the key-recovery path of reconcile_recent is exercised for real.
    Each seed gets its own database: scenario worlds reuse the same provider ids and must not meet each other."""
    from aecom import db
    from aecom.bootstrap import init
    from aecom.chaos import run_scenario

    recovered = 0
    for seed in range(6):
        db.reset_engine(f"sqlite:///{tmp_path / f'lost{seed}.db'}")
        init()
        out = run_scenario("ls-lost", seed=seed, orders=24)
        assert out["violations"] == [], (seed, out["violations"])
        recovered += out["stats"]["recovered"].get("keys", 0)
    assert recovered > 0, "no seed ever exercised the licence-key recovery path"
