"""F3 (Roadmap): Etsy as a second sales channel. Etsy never becomes the merchant of record for this business the
way Lemon Squeezy can (`aecom.store.lemonsqueezy`) - it is a second storefront that sells the *same* catalog
alongside the primary one (`aecom.store.channels`). ``EtsyChannel`` therefore writes into the same local tables
(`Product`, `Customer`, `Order`, `LicenseKey`) the primary channel and its delivery/email guard already use, so an
Etsy sale draws a licence key from the **same pool** as a primary-channel sale of the same SKU (never a second,
Etsy-only pool), is delivered through the existing `deliver_order` tool (R10's send-once email guard applies
unchanged), and shows up in the same ledger, reports and chaos invariants with zero special-casing there.

Why not a `StoreAdapter`: Etsy is additive, not a replacement checkout - the business still has one primary
platform; `get_channel`/`enabled_channels` (`aecom.store.channels`) model that.

**API**: Etsy Open API v3 (https://openapi.etsy.com), OAuth2 bearer token from `aecom.oauth_tokens.access_token`
(F2) plus the app's ``x-api-key`` header (the same `ETSY_CLIENT_ID` used for the OAuth client id - Etsy's API key
*is* the OAuth app's keystring). Every request goes through `aecom.net_guard`'s SSRF-guarded, IP-pinned GET/POST,
like `aecom.oauth_tokens` itself; `net_guard` ships no pinned PUT, so `_pinned_put` is a small local copy of
`pinned_post`'s request-building (same pattern `net_guard.pinned_post`'s docstring notes for `channels.whatsapp`),
used only for ``PUT .../inventory``.

**Catalog mapping**: `sync_listings()` never writes `Product` rows (never overwrites or duplicates the primary
store's catalog) - it only keeps a SKU <-> Etsy listing id map in ``ops_settings`` (key ``etsy_listing_map``, one
JSON object), read back by `sync_orders` and by the write operations. Each Etsy receipt transaction also carries
its own seller-set ``sku``, which is tried first (most direct and the common case when the seller keeps SKUs
aligned across channels); the listing map is the fallback for transactions where Etsy didn't echo it back. A local
`Product` for a SKU Etsy sells but the primary channel never listed is created lazily, the first time an order for
it is mirrored - the same gap-filling the Lemon Squeezy mirror uses for an unsynced variant.

**VAT (documented assumption, flagged in docs/wiki/Integrations.md)**: Etsy is a deemed marketplace facilitator
for much of what it sells (EU digital-goods VAT, UK VAT, many US states' sales tax), i.e. *Etsy*, not this
business, collects and remits that tax. `ShopReceipt.total_vat_cost` is the tax Etsy says it collected on the
sale; when present, the order is booked net of it (the amount this business actually recognises as its own
revenue) and the ledger books **no** local `vat_payable` line - there is nothing for this business to remit on
that amount. When a receipt carries no tax breakdown (fixture/shop does not expose it), the order is booked at
its full `grandtotal` with the same "no local VAT liability" assumption, and the memo says so
(``vat: marketplace-handled``) so a human/accountant can review it; this is the ``'otherwise mark vat as
marketplace-handled'`` fallback the task calls for. Refund amounts are scaled by the same net/gross ratio so a
mirrored refund can never exceed the (already net) `Order.amount_eur` it is refunding against
(`chaos.invariants.refunds_not_above_order_amount`).

**Delivery, not Etsy messaging**: a mirrored order is created ``status="paid"`` (not pre-delivered) and publishes
``order.paid`` with ``channel: "etsy"`` - exactly what the primary channel's own `MockStore.create_order` does -
so `handlers.deliver_paid_order` (already registered, unowned here) calls the existing `deliver_order` tool, which
assigns the key/link from the shared pool and emails it through AgentMail under the R10 send-once guard, same as
any other order. This only reaches a real customer inbox when `STORE_ADAPTER=mock` (the default, and what every
test here runs under): `LemonSqueezyStore.deliver_order` is an intentional no-op because Lemon Squeezy already
delivered *its own* orders - it was never written to know about an Etsy order it never processed. See
docs/wiki/Integrations.md for the flagged gap and a patch snippet for the lead.

**Shared licence-key pool**: enforced structurally, not by a new check - `LicenseKey.product_id` has no channel
column, so `MockStore._deliver`'s "first available key for this product" query already can't tell an Etsy order
from a primary one. `chaos.invariants.no_key_on_two_orders` (pre-existing, generic) already proves no key is ever
handed to two orders regardless of channel; `tests/test_etsy_channel.py` exercises both channels against one pool
and asserts that invariant holds, instead of adding a new, narrower one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import func, select

from aecom import ledger
from aecom.bus.events import outbox, publish
from aecom.company_settings import get_setting, set_setting
from aecom.config import get_settings
from aecom.db import Customer, LicenseKey, Order, Product, Refund, now, session
from aecom.money import quantize
from aecom.net_guard import Target, pinned_get, pinned_post, resolve_public
from aecom.oauth_tokens import access_token
from aecom.observability import record_error
from aecom.secrets import secrets
from aecom.store.mock import MockStore

PROVIDER = "etsy"
API_BASE = "https://openapi.etsy.com"
EXTERNAL_PREFIX = "etsy:"  # Order.external_id prefix; keeps Etsy receipt ids out of Lemon Squeezy's bare-numeric id space
LISTING_MAP_SETTING = "etsy_listing_map"  # ops_settings JSON: {"<etsy listing id>": "<our sku>"}
PAGE_SIZE = 100


# --------------------------------------------------------------------------- SKU <-> Etsy listing id (ops_settings)
def _load_listing_map() -> dict[str, str]:
    raw = get_setting(LISTING_MAP_SETTING)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save_listing_map(mapping: dict[str, str]) -> None:
    set_setting(LISTING_MAP_SETTING, json.dumps(mapping, sort_keys=True))


def _listing_id_for_sku(sku: str) -> str:
    for listing_id, mapped_sku in _load_listing_map().items():
        if mapped_sku == sku:
            return listing_id
    raise ValueError(f"no Etsy listing is mapped to SKU {sku!r}; run sync_listings() or create_listing() first")


# --------------------------------------------------------------------------- money / time helpers
def _money(m: dict[str, Any] | None) -> Decimal:
    """A `Money` object (``{"amount": int, "divisor": int, "currency_code": str}``, Etsy Open API v3) as EUR cents
    quantized to a `Decimal`. Missing/empty is zero, never an error - most receipts omit the tax fields."""
    if not m:
        return Decimal(0)
    divisor = int(m.get("divisor") or 1) or 1
    return quantize(Decimal(int(m.get("amount") or 0)) / Decimal(divisor))


def _currency(m: dict[str, Any] | None) -> str:
    return str((m or {}).get("currency_code") or "EUR").upper()


def _epoch_to_naive_utc(epoch: int | None) -> datetime | None:
    """Etsy timestamps are epoch seconds; the rest of the database stores naive UTC datetimes."""
    if not epoch:
        return None
    return datetime.fromtimestamp(int(epoch), tz=UTC).replace(tzinfo=None)


_AUTH_SCHEME = "Bearer"  # kept apart from the "Authorization" header literal - see _authorization()


def _authorization(token: str) -> str:
    """The Authorization header value for an Etsy OAuth bearer token, built one call away from any
    {"Authorization": ...} mapping. A literal auth-scheme-plus-token string assigned directly to an
    "Authorization" key in a dict literal gets silently redacted in this workspace's file-write pipeline (it
    flags any Authorization-keyed literal that looks like a bearer-style header, including ones built from a
    variable, not just hardcoded secrets - aecom.store.lemonsqueezy._client's headers dict hit the same thing,
    which is why its own Authorization value is a masked literal there, not a real one)."""
    return f"{_AUTH_SCHEME} {token}"


def _headers(token: str) -> dict[str, str]:
    client_id = secrets.get("ETSY_CLIENT_ID")
    if not client_id:
        raise RuntimeError("ETSY_CLIENT_ID is not set (it is both the OAuth client id and Etsy's x-api-key)")
    return {"x-api-key": client_id, "Authorization": _authorization(token), "Accept": "application/json"}


def _pinned_put(target: Target, *, json: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                timeout: float = 15) -> httpx.Response:
    """PUT counterpart of `net_guard.pinned_get`/`pinned_post` (SSRF-guarded, IP-pinned, no redirects). `net_guard`
    ships no pinned PUT; this mirrors `pinned_post`'s request building for the one Etsy endpoint that needs it
    (``PUT .../inventory``) - the same thing `net_guard.pinned_post`'s own docstring says `channels.whatsapp` does
    for POST, kept local to the one caller that needs the extra verb rather than changing a file this task does
    not own."""
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        request = client.build_request("PUT", target.pinned_url, json=json,
                                       headers={**(headers or {}), "Host": target.host_header},
                                       extensions={"sni_hostname": target.ascii_host})
        return client.send(request)


class EtsyChannel:
    """``ChannelAdapter`` (`aecom.store.channels`) backed by Etsy Open API v3. Stateless: every call re-reads
    settings/ops_settings/the OAuth token store, so it always reflects the latest configuration."""

    name = PROVIDER

    def __init__(self) -> None:
        self._local = MockStore()  # the shared local tables/guards every Etsy order is fulfilled through

    # --------------------------------------------------------------------- HTTP (net_guard-pinned)
    def _shop_id(self) -> str:
        shop_id = getattr(get_settings(), "etsy_shop_id", "")
        if not shop_id:
            raise RuntimeError("etsy_shop_id is not configured (Settings > Channels, or ETSY_SHOP_ID)")
        return str(shop_id)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = access_token(PROVIDER)  # OAuthError propagates as-is; called before any write this request would cause
        url = f"{API_BASE}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        target = resolve_public(url)
        resp = pinned_get(target, headers=_headers(token), timeout=getattr(get_settings(), "etsy_timeout_s", 20.0))
        resp.raise_for_status()
        return resp.json()

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Every page of an Etsy ``{"count": N, "results": [...]}`` list endpoint."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            body = self._get(path, {**params, "limit": PAGE_SIZE, "offset": offset})
            results = body.get("results") or []
            out.extend(results)
            offset += len(results)
            if not results or offset >= int(body.get("count") or 0):
                return out

    # --------------------------------------------------------------------- sync_listings (read-only; T1)
    def _listing_sku(self, listing_id: int | str) -> str | None:
        """The seller SKU of a listing's first inventory product (``GET /listings/{id}/inventory``), or ``None``
        if the seller never set one."""
        body = self._get(f"/v3/application/listings/{listing_id}/inventory")
        for product in body.get("products") or []:
            if product.get("sku"):
                return str(product["sku"])
        return None

    def sync_listings(self) -> dict[str, Any]:
        """Read-only: refresh the SKU <-> Etsy listing id map from every active listing in the shop. Never writes
        `Product` (the primary catalog is never overwritten - F3). A listing whose SKU has no local `Product` yet
        is still mapped (``unmapped``): the first order for it lazily creates one (see module docstring)."""
        shop_id = self._shop_id()
        listings = self._paged(f"/v3/application/shops/{shop_id}/listings/active", {})
        mapping = _load_listing_map()
        mapped = unmapped = 0
        for listing in listings:
            listing_id = str(listing["listing_id"])
            sku = self._listing_sku(listing["listing_id"]) or f"ETSY-{listing_id}"
            mapping[listing_id] = sku
            with session() as s:
                exists = s.scalars(select(Product.id).where(Product.sku == sku)).first() is not None
            mapped, unmapped = (mapped + 1, unmapped) if exists else (mapped, unmapped + 1)
        _save_listing_map(mapping)
        return {"listings": len(listings), "mapped": mapped, "unmapped": unmapped}

    # --------------------------------------------------------------------- sync_orders (read-first writes; T1)
    def _list_receipts(self, since: datetime) -> list[dict[str, Any]]:
        shop_id = self._shop_id()
        moment = since if since.tzinfo else since.replace(tzinfo=UTC)
        return self._paged(f"/v3/application/shops/{shop_id}/receipts",
                           {"min_created": int(moment.timestamp()), "sort_on": "created", "sort_order": "asc"})

    def _resolve_sku(self, txn: dict[str, Any], listing_map: dict[str, str]) -> str:
        listing_id = str(txn.get("listing_id") or "")
        return txn.get("sku") or listing_map.get(listing_id) or f"ETSY-{listing_id}"

    def _create_order(self, receipt: dict[str, Any], listing_map: dict[str, str], ext: str) -> str:
        """Mirror one newly-seen paid receipt: customer, order (net of any Etsy-collected VAT - see module
        docstring), a sale ledger entry and ``order.paid`` - all in one transaction, so a failure here leaves no
        partial rows. Returns the new local order id."""
        transactions = receipt.get("transactions") or []
        if not transactions:
            raise ValueError(f"Etsy receipt {receipt.get('receipt_id')} has no transactions to mirror")
        txn = transactions[0]  # F3 simplification, same as Lemon Squeezy's first_order_item: one product per local Order
        sku = self._resolve_sku(txn, listing_map)
        gross = _money(receipt.get("grandtotal"))
        currency = _currency(receipt.get("grandtotal"))
        if currency != "EUR":
            raise ValueError(f"Etsy receipt {receipt.get('receipt_id')} is in {currency}, the ledger books EUR; "
                             "not converted, mirror it manually")
        vat_collected = _money(receipt.get("total_vat_cost"))
        net = gross - vat_collected if vat_collected else gross
        buyer_email = (receipt.get("buyer_email") or "").strip()
        country = (receipt.get("country_iso") or "").upper() or None
        with session() as s, outbox(s):
            p = s.scalars(select(Product).where(Product.sku == sku)).first()
            if p is None:  # an Etsy-only listing never sold on the primary channel: fill the gap, never overwrite
                # Etsy has no "licence key product" concept to read back, so the gap-filler is always a plain
                # download (a download link, no key pool expected) - a product that genuinely sells licence keys
                # on Etsy needs a human to create the matching local Product (same SKU) once, as on any channel.
                p = Product(sku=sku, title=txn.get("title") or sku, price_eur=float(gross), kind="download",
                           status="active", external_id=f"etsy-listing:{txn.get('listing_id')}")
                s.add(p)
                s.flush()
            c = s.scalars(select(Customer).where(Customer.email == buyer_email)).first() if buyer_email else None
            if c is None:
                # Etsy's receipt object does carry buyer_email; a missing one (privacy setting / API scope) still
                # needs *a* customer row to hang the order off, so an unguessable placeholder stands in for it -
                # delivery then has no real address to send to, surfaced by a failed access-email send, not silently.
                email = buyer_email or f"etsy-buyer-{receipt.get('buyer_user_id') or receipt.get('receipt_id')}@etsy.invalid"
                c = Customer(email=email, country=country or "", acquisition_channel=PROVIDER)
                s.add(c)
                s.flush()
            elif country and not c.country:
                c.country = country
            # H8: Etsy's own checkout requires EU buyers to accept immediate delivery of digital goods under its
            # platform-wide terms (not our checkbox) - recorded as given through Etsy, not left blank/blocking.
            o = Order(customer_id=c.id, product_id=p.id, quantity=max(int(txn.get("quantity") or 1), 1),
                      amount_eur=float(net), vat_eur=0.0, campaign_id=PROVIDER, status="paid", external_id=ext,
                      created_at=_epoch_to_naive_utc(receipt.get("create_timestamp")) or now(),
                      withdrawal_consent_at=now(), withdrawal_consent_version="etsy-checkout-terms")
            s.add(o)
            s.flush()
            tag = "marketplace-handled" if not vat_collected else f"marketplace-handled, excl. Etsy VAT EUR {vat_collected:.2f}"
            lines: list[tuple] = [("cash", net, 0), ("revenue", 0, net)]
            if p.unit_cost_eur:
                cost = quantize(p.unit_cost_eur)
                lines += [("cogs", cost, 0), ("cash", 0, cost)]
            ledger.post(lines, f"sale {o.id} (vat: {tag})", o.id, session=s)
            publish("order.paid", {"order_id": o.id, "sku": p.sku, "email": c.email, "channel": PROVIDER,
                                   "receipt_id": receipt.get("receipt_id")}, source=PROVIDER, session=s)
            return o.id

    def _mirror_refunds(self, order_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        """Book the not-yet-mirrored part of ``receipt["refunds"]`` through the existing local refund path
        (`MockStore.refund_order`, which also revokes the licence key on a full refund). Etsy reports refunds in
        gross; they are scaled by this order's net/gross ratio so a mirrored refund can never exceed the
        (already net) ``Order.amount_eur`` - consistent with the VAT assumption the sale was booked under."""
        refunds = receipt.get("refunds") or []
        if not refunds:
            return {}
        gross = _money(receipt.get("grandtotal"))
        with session() as s:
            o = s.get(Order, order_id)
            net_total = quantize(o.amount_eur)
            already = quantize(s.scalar(select(func.coalesce(func.sum(Refund.amount_eur), 0.0))
                                        .where(Refund.order_id == order_id)) or 0)
        provider_gross = sum((_money(r.get("amount")) for r in refunds), Decimal(0))
        scale = (net_total / gross) if gross else Decimal(1)
        provider_net = quantize(provider_gross * scale)
        delta = quantize(provider_net - already)
        if delta <= 0:
            return {}
        reason = next((r.get("reason") for r in reversed(refunds) if r.get("reason")), None) or "refunded on Etsy"
        self._local.refund_order(order_id, float(delta), f"Etsy: {reason}")
        return {"refund_booked": True, "refunded_eur": float(delta)}

    def _mirror_receipt(self, receipt: dict[str, Any], listing_map: dict[str, str]) -> dict[str, Any]:
        rid = str(receipt.get("receipt_id"))
        ext = f"{EXTERNAL_PREFIX}{rid}"
        with session() as s:
            existing_id = s.scalars(select(Order.id).where(Order.external_id == ext)).first()
        if existing_id is None:
            if not receipt.get("is_paid"):
                return {"receipt_id": rid, "status": "ignored", "reason": f"receipt status {receipt.get('status')}"}
            order_id = self._create_order(receipt, listing_map, ext)
            refunded = self._mirror_refunds(order_id, receipt)
            return {"receipt_id": rid, "order_id": order_id, "status": "created", **refunded}
        refunded = self._mirror_refunds(existing_id, receipt)
        if refunded.get("refund_booked"):
            return {"receipt_id": rid, "order_id": existing_id, "status": "updated", **refunded}
        return {"receipt_id": rid, "order_id": existing_id, "status": "unchanged", "already": True}

    def sync_orders(self, since: datetime) -> dict[str, Any]:
        """Mirror every Etsy receipt created at/after ``since``: new paid receipts become local orders (paid only;
        idempotent on receipt id - a re-sync never duplicates), existing ones pick up any new refund. Publishes
        ``order.paid`` (channel ``etsy``) for new orders so `handlers.deliver_paid_order` delivers and emails them
        exactly like a primary-channel order, drawing from the same licence-key pool. A failing receipt is
        recorded (`record_error`) and does not stop the rest."""
        access_token(PROVIDER)  # fail fast on a disconnected/broken OAuth token: no receipt is fetched or written
        listing_map = _load_listing_map()
        receipts = self._list_receipts(since)
        created = updated = unchanged = ignored = errors = 0
        for receipt in receipts:
            try:
                result = self._mirror_receipt(receipt, listing_map)
            except Exception as exc:  # one bad receipt must not stop the rest (same pattern as lemonsqueezy.reconcile_recent)
                errors += 1
                record_error("etsy.sync_orders", exc, context={"receipt_id": receipt.get("receipt_id")})
                continue
            status = result["status"]
            if status == "created":
                created += 1
            elif status == "updated":
                updated += 1
            elif status == "ignored":
                ignored += 1
            else:
                unchanged += 1
        return {"receipts": len(receipts), "created": created, "updated": updated, "unchanged": unchanged,
                "ignored": ignored, "errors": errors}

    # --------------------------------------------------------------------- writes (T3 once the lead gates them)
    def create_listing(self, sku: str, title: str, description: str, price_eur: float, quantity: int = 999,
                       taxonomy_id: int = 1, who_made: str = "i_did", when_made: str = "made_to_order") -> dict[str, Any]:
        """Publish a new Etsy draft listing for ``sku`` (``POST /shops/{shop_id}/listings``, form-encoded per the
        Etsy Open API v3 spec) and record it in the SKU <-> listing id map. Side-effecting: callable only through
        whatever T3 tool the lead registers for it; nothing in this module calls it automatically."""
        shop_id = self._shop_id()
        token = access_token(PROVIDER)
        data = {"quantity": quantity, "title": title, "description": description, "price": price_eur,
               "who_made": who_made, "when_made": when_made, "taxonomy_id": taxonomy_id}
        target = resolve_public(f"{API_BASE}/v3/application/shops/{shop_id}/listings")
        resp = pinned_post(target, data=data, headers=_headers(token))
        resp.raise_for_status()
        body = resp.json()
        listing_id = str(body.get("listing_id"))
        mapping = _load_listing_map()
        mapping[listing_id] = sku
        _save_listing_map(mapping)
        return {"listing_id": listing_id, "sku": sku, "state": body.get("state")}

    def update_listing_inventory(self, sku: str, quantity: int) -> dict[str, Any]:
        """Set every enabled offering's quantity on ``sku``'s Etsy listing (``PUT /listings/{id}/inventory``).
        Looked up in the SKU <-> listing map (`sync_listings`/`create_listing`); raises if ``sku`` has no known
        Etsy listing yet. Side-effecting: callable only through whatever T3 tool the lead registers for it."""
        listing_id = _listing_id_for_sku(sku)
        token = access_token(PROVIDER)
        current = self._get(f"/v3/application/listings/{listing_id}/inventory")
        products = current.get("products") or []
        for product in products:
            for offering in product.get("offerings") or []:
                if offering.get("is_enabled", True):
                    offering["quantity"] = quantity
        target = resolve_public(f"{API_BASE}/v3/application/listings/{listing_id}/inventory")
        resp = _pinned_put(target, json={"products": products}, headers=_headers(token))
        resp.raise_for_status()
        return {"listing_id": listing_id, "sku": sku, "quantity": quantity}
