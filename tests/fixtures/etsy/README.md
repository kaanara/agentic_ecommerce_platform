# Etsy fixtures (offline)

**Recorded-shape, not from a live shop.** These JSON files were written by hand from Etsy's public Open API v3
schema (`https://www.etsy.com/openapi/generated/oas/3.0.0.json`, fetched 2026-10-30) and its reference docs
(https://developers.etsy.com/documentation/reference) - field names, nesting and the `Money` shape match that
schema, but the values (ids, amounts, emails, addresses) are invented for `tests/test_etsy_channel.py`. No test in
this repository calls the real Etsy API.

| File | Shape | Etsy schema object |
|---|---|---|
| `listings_active.json` | `GET /v3/application/shops/{shop_id}/listings/active` | `ShopListings` (`{count, results: [ShopListing]}`) |
| `listing_inventory.json` | `GET /v3/application/listings/{listing_id}/inventory`, keyed by listing id for the fake transport | `ListingInventory` (`{products: [ListingInventoryProduct]}`) |
| `receipts.json` | `GET /v3/application/shops/{shop_id}/receipts` - first poll, two paid receipts, no refunds yet | `ShopReceipts` (`{count, results: [ShopReceipt]}`) |
| `receipts_refund_partial.json` | same first receipt, a later poll with one `refunds[]` entry | `ShopReceipts` with `ShopReceipt.refunds: [ShopRefund]` |
| `receipts_refund_full.json` | same receipt again, a second refund entry that brings the total to the full order | as above |
| `receipts_unpaid.json` | a receipt that has not been paid yet (`is_paid: false`) | `ShopReceipts` |
| `create_listing_response.json` | response of `POST /v3/application/shops/{shop_id}/listings` | `ShopListing` |

All money fields are Etsy's `Money` object (`{"amount": int, "divisor": int, "currency_code": str}`; EUR cents here
since `divisor: 100`), never a bare float.

Points the schema leaves to this integration's own judgement (see `src/aecom/store/etsy.py`'s module docstring for
the fuller reasoning, and `docs/wiki/Integrations.md` for the human-facing version):
- **VAT / marketplace-facilitator tax:** `ShopReceipt.total_vat_cost` is what Etsy says it already collected and
  remits itself. The first fixture receipt (`LIC-SHARED-001`) carries one (EUR 10.00 on a EUR 50.00 order, so the
  order is booked net at EUR 40.00, `vat: marketplace-handled, excl. Etsy VAT EUR 10.00` in the ledger memo); the
  second (`ETSY-ONLY-002`) carries a zero one, exercising the `'otherwise mark vat as marketplace-handled'`
  fallback (booked at its full EUR 15.00, memo `vat: marketplace-handled`). Neither books a local `vat_payable`
  line - the assumption is that this business never itself owes or remits VAT on an Etsy sale.
- **Refunds are individual events, not a cumulative total** (unlike Lemon Squeezy's `refunded_amount`): each
  `ShopReceipt.refunds[]` entry is one refund's own `Money`, so the adapter sums the list itself to get the
  provider's cumulative total before comparing it against what is already mirrored locally.
- **`buyer_email`** is a real `ShopReceipt` field (confirmed against the schema above), unlike Etsy's legacy v2 API
  which did not expose it; a shop without `email_r` scope approved, or a receipt where Etsy withholds it, would
  need a documented fallback (`etsy-buyer-<id>@etsy.invalid`) - never a channel without *a* customer row to
  deliver against.
- **One product per order:** like Lemon Squeezy's `first_order_item`, a receipt's first `transactions[]` entry is
  what is mirrored to a local `Order`, since this app's `Order` model is one product per order on every channel.
  A genuinely multi-item Etsy receipt would need a human/accountant to reconcile the remainder.
- **Licence key in `create_listing_response.json`:** Etsy has no "licence key product" concept; a listing this
  integration creates for a channel-only SKU is never assumed to need one (see `src/aecom/store/etsy.py`).
