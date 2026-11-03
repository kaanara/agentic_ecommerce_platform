"""Allowlisted Whop operations behind the agents' generic ``whop_request`` tool.

Agents get the breadth of Whop's CLI (products, variants, promo codes, checkout links, account preferences) without
a shell or a second binary: ``whop_request(operation, args)``. ``OPS`` is the whole surface. An operation that is not
in this table does not exist for agents, and anything dangerous is left out on purpose (payments refund/void/create
- refunds go through ``refund_order`` so the mirror stays correct -, webhooks, API keys, payouts, transfers, cards,
ledgers, ads, account delete/transfer).

Each operation has a policy name ``api:whop/<operation>``. ``policy.yaml`` gives it a tier (reads T0, drafts and text
T1, promo codes T2 with a limit, publishing/archiving/preferences T3), and ``api:whop/*`` is a T3 catch-all for
anything added here without a rule. ``args`` is one flat dict: path parameters, then query (GET/DELETE) or body
(POST/PATCH). The gate sees this flat dict, so limits (``amount_off``) and the approval card show exactly what runs.

Paths come from Whop's OpenAPI spec (https://api.whop.com/api/v1/openapi.json); ``tests/fixtures/whop/
openapi_operations.json`` is a saved list of its operations and a test checks every path here against it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aecom.config import get_settings

PREFIX = "api:whop/"
MAX_RESULT_CHARS = 6000

PRODUCT_TEXT = {"title", "description", "headline", "labels", "metadata", "gallery_images", "banner_image",
                "send_welcome_message"}
VARIANT_EDIT = {"title", "description", "sku", "stock", "unlimited_stock", "trial_period_days", "expiration_days",
                "internal_notes", "metadata", "attributes"}
VARIANT_CREATE = VARIANT_EDIT | {"product_id", "currency", "billing_type", "billing_period", "initial_price",
                                 "renewal_price", "release_method", "allow_multiple_quantity"}
PROMO_FIELDS = {"code", "amount_off", "base_currency", "promo_type", "new_users_only", "promo_duration_months",
                "product_id", "variant_ids", "stock", "unlimited_stock", "expires_at", "one_per_customer",
                "churned_users_only", "existing_memberships_only"}
CHECKOUT_FIELDS = {"variant_id", "metadata", "redirect_url", "affiliate_code", "campaign_id"}
# preferences that only change how the shop talks to buyers; payouts, ads, cards, dispute fighter, settlement
# currency and economic intelligence are money settings and stay human-only
PREFERENCE_KEYS = {"customer_tracking", "feedback_options", "review_request_emails", "subscription_failure_behavior"}
PRICE_FIELDS = {"initial_price", "renewal_price", "strike_through_initial_price", "strike_through_renewal_price"}


class OpError(ValueError):
    """An invalid or forbidden operation; the message is the tool result the agent sees."""


@dataclass(frozen=True)
class Op:
    method: str
    path: str
    side_effect: bool
    summary: str
    allowed: frozenset[str] | None = None  # None = any argument; else only these (path params are always allowed)
    guard: Callable[[dict[str, Any]], None] | None = None
    fixed: dict[str, Any] = field(default_factory=dict)  # body values forced by the operation
    inject_account: bool = False  # add the configured account id (query for GET, body for POST)

    @property
    def path_params(self) -> list[str]:
        return re.findall(r"{(\w+)}", self.path)


def _no_price(args: dict[str, Any]) -> None:
    hit = sorted(PRICE_FIELDS & set(args))
    if hit:
        raise OpError(f"{', '.join(hit)} cannot be changed here: use update_price (it applies the price-change limits).")


def _promo(args: dict[str, Any]) -> None:
    missing = [k for k in ("code", "amount_off", "promo_type") if k not in args]
    if missing:
        raise OpError(f"promo_codes.create needs {', '.join(missing)}")
    if args["promo_type"] not in ("percentage", "flat_amount"):
        raise OpError("promo_type must be percentage or flat_amount")
    if not 0 < float(args["amount_off"]) <= (100 if args["promo_type"] == "percentage" else 10_000):
        raise OpError("amount_off is out of range")
    args.setdefault("base_currency", get_settings().whop_currency.lower())
    args.setdefault("new_users_only", False)
    args.setdefault("promo_duration_months", 1)


def _checkout(args: dict[str, Any]) -> None:
    if "variant_id" not in args:
        raise OpError("checkout_configurations.create needs variant_id (plan_...)")
    campaign = args.pop("campaign_id", None)
    if campaign:
        args["metadata"] = {**(args.get("metadata") or {}), "campaign_id": str(campaign)}


def _preferences(args: dict[str, Any]) -> None:
    bad = sorted(set(args) - PREFERENCE_KEYS - {"account_id"})
    if bad:
        raise OpError(f"preferences {', '.join(bad)} are human-only; agents may change {', '.join(sorted(PREFERENCE_KEYS))}")


def _read(path: str, summary: str, *, account: bool = False) -> Op:
    return Op("GET", path, False, summary, inject_account=account)


OPS: dict[str, Op] = {
    # ---- reads (T0)
    "accounts.me": _read("/accounts/me", "The store account: name, country, balances, capabilities"),
    "accounts.preferences": _read("/accounts/{account_id}/preferences", "Store preferences"),
    "products.list": _read("/products", "Products (first, after, query, visibilities)", account=True),
    "products.get": _read("/products/{id}", "One product with its variants"),
    "variants.list": _read("/variants", "Variants/prices (product_ids, visibilities)", account=True),
    "variants.get": _read("/variants/{id}", "One variant"),
    "payments.list": _read("/payments", "Payments (statuses, created_after, product_ids)", account=True),
    "payments.get": _read("/payments/{id}", "One payment"),
    "refunds.list": _read("/refunds", "Refunds"),
    "refunds.get": _read("/refunds/{id}", "One refund"),
    "promo_codes.list": _read("/promo_codes", "Promo codes", account=True),
    "promo_codes.get": _read("/promo_codes/{id}", "One promo code"),
    "checkout_configurations.list": _read("/checkout_configurations", "Checkout links", account=True),
    "checkout_configurations.get": _read("/checkout_configurations/{id}", "One checkout link"),
    "memberships.list": _read("/memberships", "Memberships (buyers' access)"),
    "memberships.get": _read("/memberships/{id}", "One membership"),
    # ---- catalog text and stock (T1)
    "products.update": Op("PATCH", "/products/{id}", True, "Edit title/description/headline/labels/images/metadata. "
                          "Visibility is changed with products.publish / unpublish / archive.",
                          allowed=frozenset(PRODUCT_TEXT)),
    "variants.create": Op("POST", "/variants", True, "Add a hidden variant (price at creation, hidden until published)",
                          allowed=frozenset(VARIANT_CREATE), fixed={"visibility": "hidden"}, inject_account=True),
    "variants.update": Op("PATCH", "/variants/{id}", True, "Edit title/sku/stock/trial/notes (prices: update_price)",
                          allowed=frozenset(VARIANT_EDIT), guard=_no_price),
    # ---- publishing and removal (T3)
    "products.publish": Op("POST", "/products/{id}/publish", True, "Make a product public"),
    "products.unpublish": Op("POST", "/products/{id}/unpublish", True, "Hide a product"),
    "products.archive": Op("PATCH", "/products/{id}", True, "Archive a product", allowed=frozenset(),
                           fixed={"visibility": "archived"}),
    "variants.archive": Op("PATCH", "/variants/{id}", True, "Archive a variant", allowed=frozenset(),
                           fixed={"visibility": "archived"}),
    "promo_codes.delete": Op("DELETE", "/promo_codes/{id}", True, "Delete a promo code"),
    # ---- promo codes and checkout links
    "promo_codes.create": Op("POST", "/promo_codes", True, "Create a promo code (percentage or flat amount)",
                             allowed=frozenset(PROMO_FIELDS), guard=_promo, inject_account=True),
    "promo_codes.activate": Op("POST", "/promo_codes/{id}/activate", True, "Resume a promo code"),
    "promo_codes.deactivate": Op("POST", "/promo_codes/{id}/deactivate", True, "Pause a promo code"),
    "checkout_configurations.create": Op("POST", "/checkout_configurations", True,
                                         "Create a checkout link for a variant (purchase_url in the result); "
                                         "campaign_id is stored as metadata and reaches the mirrored order",
                                         allowed=frozenset(CHECKOUT_FIELDS), guard=_checkout, inject_account=True),
    # ---- store preferences (T3)
    "accounts.update_preferences": Op("PATCH", "/accounts/{account_id}/preferences", True,
                                      "Change buyer-facing preferences (customer tracking page, cancel reasons, "
                                      "review emails, failed-renewal behaviour)", guard=_preferences),
}


def describe() -> str:
    """The operation list shown to the model in the tool description."""
    return "\n".join(f"- {name}: {op.summary}" + (f" [{', '.join(op.path_params)}]" if op.path_params else "")
                     for name, op in OPS.items())


def prepare(operation: str, args: dict[str, Any] | None, account_id: str | None = None) -> tuple[Op, str, dict[str, Any]]:
    """Validate ``operation`` and ``args`` and build (op, concrete path, params-or-body). Raises ``OpError``."""
    op = OPS.get(operation)
    if op is None:
        raise OpError(f"unknown operation {operation!r}. Available: {', '.join(OPS)}")
    args = dict(args or {})
    path_vals = {}
    for name in op.path_params:
        if name == "account_id" and name not in args and account_id:
            args["account_id"] = account_id
        if not args.get(name):
            raise OpError(f"{operation} needs {name}")
        path_vals[name] = str(args.pop(name))
    if op.allowed is not None:
        bad = sorted(set(args) - op.allowed - {"account_id"})
        if bad:
            raise OpError(f"{operation} does not accept {', '.join(bad)}. Accepted: "
                          f"{', '.join(sorted(op.allowed)) or 'only the path parameters'}")
    if op.inject_account:
        args.pop("account_id", None)  # always the configured store, never one the model picked
    if op.guard:
        op.guard(args)
    args.update(op.fixed)
    if op.inject_account and account_id:
        args["account_id"] = account_id
    return op, op.path.format(**path_vals), args


def policy_name(operation: str) -> str:
    return PREFIX + operation


def trim(result: Any) -> str:
    """The API response as compact JSON for the model: buyer email addresses masked, capped in length."""
    def mask(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: (_mask_email(v) if k in ("customer_email", "email") and isinstance(v, str) else mask(v))
                    for k, v in x.items() if k not in ("client_secret", "recovery_url", "ip_address", "user_agent")}
        if isinstance(x, list):
            return [mask(i) for i in x]
        return x
    text = json.dumps(mask(result), default=str, separators=(",", ":"))
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + '..."(truncated; narrow the query)'


def _mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def execute(operation: str, args: dict[str, Any] | None, *, store: Any = None) -> str:
    """Run one operation against Whop (called by the tool after the gate allows it, or by ``approvals`` after a
    human approved it). After a catalog write the touched product is refreshed into the local mirror."""
    from aecom.store.whop import WhopStore

    store = store or WhopStore()
    cfg = get_settings()
    op, path, payload = prepare(operation, args, cfg.whop_account_id)
    if op.method == "GET":
        result = store.request("GET", path, params=payload)
    else:
        result = store.request(op.method, path, body=payload or None)
    _refresh_mirror(store, operation, args or {}, result)
    return trim(result)


def _refresh_mirror(store: Any, operation: str, args: dict[str, Any], result: Any) -> None:
    """Keep the local catalog in step with a write (best effort: Whop already holds the truth, and the next
    sync_catalog fixes any gap)."""
    try:
        if operation.startswith("variants.") and operation != "variants.list":
            variant_id = args.get("id") or (result or {}).get("id")  # create returns the new variant, update has the id
            if variant_id:
                store.resync_variant(str(variant_id))
        elif operation.startswith("products.") and operation not in ("products.list", "products.get"):
            store.resync_product(str(args.get("id") or (result or {}).get("id")))
    except Exception:  # noqa: BLE001 - a mirror refresh failure must not hide a write that did succeed
        from aecom.observability import record_error

        record_error("whop.mirror_refresh", RuntimeError(f"{operation} done; local mirror not refreshed"))
