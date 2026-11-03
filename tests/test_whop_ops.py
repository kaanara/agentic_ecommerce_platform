"""Whop operations behind the agents' ``whop_request`` tool: the allowlist, argument guards, policy tiers, the gated
tool and the approval path. Offline (MockTransport)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from aecom import approvals
from aecom.db import Approval, Product, session
from aecom.policy.gate import evaluate, load_policy
from aecom.registry import whop_tool
from aecom.registry.tools import build_tools
from aecom.store import whop, whop_ops
from aecom.store.whop_ops import OPS, OpError, prepare

FIX = Path(__file__).parent / "fixtures" / "whop"


@pytest.fixture(autouse=True)
def _policy_cache():
    load_policy.cache_clear()
    yield
    load_policy.cache_clear()


@pytest.fixture
def served(monkeypatch, fresh_company):
    """Store adapter whop + a fake API; every request is recorded in `calls`."""
    monkeypatch.setenv("WHOP_API_KEY", "whop-test-key")
    monkeypatch.setattr(fresh_company, "store_adapter", "whop")
    monkeypatch.setattr(fresh_company, "whop_account_id", "biz_store")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if path == "/api/v1/products":
            return httpx.Response(200, json={"data": [{"id": "prod_1", "title": "T", "customer_email": "eva@example.nl"}]})
        if path.startswith("/api/v1/variants/plan_") and request.method == "GET":
            return httpx.Response(200, json={"id": "plan_1", "title": "V", "visibility": "hidden", "initial_price": 9.0,
                                             "billing_type": "one_time", "product": {"id": "prod_1"}})
        if path == "/api/v1/products/prod_1":
            return httpx.Response(200, json={"id": "prod_1", "title": "T", "visibility": "hidden", "variants": [{}]})
        return httpx.Response(200, json={"id": "x", "ok": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(whop.WhopStore, "__init__", lambda self, transport_=None: _init(self, transport))
    return calls


def _init(self, transport):
    from aecom.config import get_settings
    from aecom.store.mock import MockStore

    self.settings, self._transport, self._local = get_settings(), transport, MockStore()


# --------------------------------------------------------------------------- the table
def test_every_path_exists_in_the_saved_openapi_spec():
    spec = set(json.loads((FIX / "openapi_operations.json").read_text())["operations"])
    for name, op in OPS.items():
        template = op.path.replace("{account_id}", "{account_id}")
        assert f"{op.method} {template}" in spec or f"{op.method} {template.replace('{id}', '{id}')}" in spec, name


def test_no_dangerous_operation_is_exposed():
    for name, op in OPS.items():
        if op.method != "GET":
            assert not any(w in op.path for w in ("/refund", "/void", "/capture", "/retry", "/payouts", "/transfer",
                                                  "/api_keys", "/webhooks", "/form_company", "/suspend")), name
        assert not any(w in op.path for w in ("/payouts", "/api_keys", "/webhooks", "/cards", "/ledgers")), name
        assert not (op.method == "DELETE" and "/accounts" in op.path), name
        assert not (op.method != "GET" and op.path.startswith(("/payments", "/refunds", "/memberships"))), name


def test_unknown_operation_lists_the_available_ones():
    with pytest.raises(OpError, match="unknown operation 'payments.refund'.*products.list"):
        prepare("payments.refund", {"id": "pay_1"})


def test_read_injects_the_account_and_requires_path_params():
    op, path, query = prepare("products.list", {"query": "x", "account_id": "biz_other"}, "biz_store")
    assert (op.method, path, query) == ("GET", "/products", {"query": "x", "account_id": "biz_store"})
    with pytest.raises(OpError, match="needs id"):
        prepare("products.get", {})


def test_products_update_allows_text_but_not_visibility():
    _, path, body = prepare("products.update", {"id": "prod_1", "title": "New"}, "biz_store")
    assert (path, body) == ("/products/prod_1", {"title": "New"})
    with pytest.raises(OpError, match="does not accept visibility"):
        prepare("products.update", {"id": "prod_1", "visibility": "visible"})


def test_variants_update_refuses_prices_and_visibility():
    prepare("variants.update", {"id": "plan_1", "stock": 5})
    for key in ("initial_price", "renewal_price"):
        with pytest.raises(OpError):
            prepare("variants.update", {"id": "plan_1", key: 1.0})
    with pytest.raises(OpError, match="visibility"):
        prepare("variants.update", {"id": "plan_1", "visibility": "visible"})


def test_variants_create_is_always_hidden_and_uses_the_configured_account():
    _, _, body = prepare("variants.create", {"product_id": "prod_1", "initial_price": 5, "account_id": "biz_evil"},
                         "biz_store")
    assert body["visibility"] == "hidden" and body["account_id"] == "biz_store"
    with pytest.raises(OpError):
        prepare("variants.create", {"product_id": "prod_1", "visibility": "visible"})


def test_archive_and_publish_take_no_free_arguments():
    _, path, body = prepare("products.archive", {"id": "prod_1"})
    assert (path, body) == ("/products/prod_1", {"visibility": "archived"})
    with pytest.raises(OpError):
        prepare("products.archive", {"id": "prod_1", "title": "sneaky"})


def test_promo_code_defaults_and_validation():
    _, _, body = prepare("promo_codes.create", {"code": "SAVE10", "amount_off": 10, "promo_type": "percentage"}, "biz_store")
    assert body["base_currency"] == "eur" and body["new_users_only"] is False and body["account_id"] == "biz_store"
    for bad in ({"code": "X", "amount_off": 150, "promo_type": "percentage"},
                {"code": "X", "amount_off": 0, "promo_type": "percentage"},
                {"code": "X", "promo_type": "percentage"},
                {"code": "X", "amount_off": 5, "promo_type": "bogus"}):
        with pytest.raises(OpError):
            prepare("promo_codes.create", bad)


def test_checkout_link_stores_campaign_as_metadata():
    _, _, body = prepare("checkout_configurations.create", {"variant_id": "plan_1", "campaign_id": "c-9"}, "biz_store")
    assert body == {"variant_id": "plan_1", "metadata": {"campaign_id": "c-9"}, "account_id": "biz_store"}
    with pytest.raises(OpError, match="variant_id"):
        prepare("checkout_configurations.create", {})


def test_preferences_allow_only_buyer_facing_keys():
    _, path, body = prepare("accounts.update_preferences", {"review_request_emails": False}, "biz_store")
    assert path == "/accounts/biz_store/preferences" and body == {"review_request_emails": False}
    for key in ("preferred_settlement_currency", "dispute_fighter_enabled", "cards_auto_top_up", "ads_payment_methods"):
        with pytest.raises(OpError, match="human-only"):
            prepare("accounts.update_preferences", {key: True}, "biz_store")


def test_trim_masks_emails_and_secrets_and_caps_length():
    out = whop_ops.trim({"customer_email": "eva@example.nl", "client_secret": "s", "n": [{"email": "a@b.c"}]})
    assert "eva@" not in out and "e***@example.nl" in out and "client_secret" not in out and "a***@b.c" in out
    assert whop_ops.trim({"x": "y" * 20000}).endswith('(truncated; narrow the query)')


# --------------------------------------------------------------------------- policy tiers
def tier(agent, op, args=None):
    return evaluate(agent, whop_ops.policy_name(op), args or {}, side_effect=OPS[op].side_effect)


def test_reads_are_t0_for_the_commerce_agents_only():
    assert tier("store_ops", "products.list").decision == "allow"
    assert tier("finance", "payments.list").decision == "allow"
    assert tier("support", "payments.list").decision == "deny"


def test_drafts_text_and_checkout_links_run_unattended():
    assert tier("store_ops", "products.update").decision == "allow"
    assert tier("store_ops", "variants.create").decision == "allow"
    assert tier("marketing", "checkout_configurations.create").decision == "allow"
    assert tier("marketing", "products.update").decision == "deny"  # not marketing's


def test_promo_codes_escalate_above_the_discount_limit():
    assert tier("marketing", "promo_codes.create", {"amount_off": 20}).decision == "allow"
    over = tier("marketing", "promo_codes.create", {"amount_off": 50})
    assert over.decision == "escalate" and "amount_off=50" in over.reason


@pytest.mark.parametrize("op", ["products.publish", "products.unpublish", "products.archive", "variants.archive",
                                "promo_codes.delete", "accounts.update_preferences"])
def test_publishing_archiving_and_preferences_need_a_human(op):
    assert tier("store_ops", op, {"id": "x"}).decision == "escalate"


def test_an_unclassified_whop_operation_falls_on_the_t3_catch_all():
    assert evaluate("store_ops", "api:whop/brand_new_op", {}, side_effect=True).decision == "escalate"


def test_every_operation_has_a_rule_so_the_catch_all_is_only_a_safety_net():
    rules = load_policy()["rules"]
    for name in OPS:
        assert f"api:whop/{name}" in rules, name


# --------------------------------------------------------------------------- the tool
def test_the_tool_is_offered_only_with_the_whop_store(served, monkeypatch, fresh_company):
    names = [t.name for t in asyncio.run(build_tools("store_ops"))]
    assert "whop_request" in names
    monkeypatch.setattr(fresh_company, "store_adapter", "mock")
    assert "whop_request" not in [t.name for t in asyncio.run(build_tools("store_ops"))]


def call(agent, operation, args=None):
    tool = whop_tool.build_whop_tool(agent)
    return asyncio.run(tool.coroutine(operation=operation, args=args))


def test_an_allowed_read_runs_and_masks_buyer_data(served):
    out = call("store_ops", "products.list", {"query": "t"})
    assert json.loads(out)["data"][0]["id"] == "prod_1" and "eva@example" not in out
    assert served[0].url.params["account_id"] == "biz_store"


def test_a_write_carries_an_idempotency_key_and_refreshes_the_mirror(served):
    call("store_ops", "variants.update", {"id": "plan_1", "stock": 3})
    patch = [c for c in served if c.method == "PATCH"][0]
    assert patch.url.path == "/api/v1/variants/plan_1" and json.loads(patch.content) == {"stock": 3}
    assert patch.headers["Idempotency-Key"].startswith("aecom-")
    with session() as s:
        assert s.scalars(select(Product.external_id)).all() == ["plan_1"]  # variant refreshed into the local catalog


def test_an_invalid_call_is_an_error_result_not_a_crash_and_sends_nothing(served):
    assert call("store_ops", "payments.refund", {"id": "p"}).startswith("ERROR: unknown operation")
    assert call("store_ops", "variants.update", {"id": "plan_1", "initial_price": 1}).startswith("ERROR:")
    assert served == []


def test_publishing_parks_an_approval_and_the_approved_call_runs(served):
    msg = call("store_ops", "products.publish", {"id": "prod_1"})
    assert msg.startswith("ESCALATED to human approval (T3")
    assert [c for c in served if c.method == "POST"] == []  # nothing sent yet
    with session() as s:
        a = s.scalars(select(Approval)).one()
        assert (a.tool_name, a.args) == ("api:whop/products.publish", {"id": "prod_1"})
        approval_id = a.id
    approvals.approve(approval_id, "alice")
    posts = [c for c in served if c.method == "POST"]
    assert posts[0].url.path == "/api/v1/products/prod_1/publish"


def test_the_kill_switch_stops_whop_writes(served, monkeypatch):
    from aecom.company_settings import set_setting

    set_setting("kill_switch", "on")
    assert call("store_ops", "variants.update", {"id": "plan_1", "stock": 1}).startswith("DENIED")
    assert served == []
