# Lemon Squeezy fixtures (offline)

These JSON files were written by hand to match the shapes in the Lemon Squeezy docs (read on 2026-10-27).
`tests/test_lemonsqueezy.py` uses them, so no test calls the real API.

| File | Shape | Source |
|---|---|---|
| `products.json` | `GET /v1/products?filter[store_id]=…` (JSON:API list of product objects) | https://docs.lemonsqueezy.com/api/products/the-product-object |
| `variants.json` | `GET /v1/variants?filter[product_id]=…` (list of variant objects, `meta.page.lastPage`) | https://docs.lemonsqueezy.com/api/variants/list-all-variants |
| `order_created.json` | webhook body: `meta` + `data` (order object) | https://docs.lemonsqueezy.com/help/webhooks/webhook-requests, https://docs.lemonsqueezy.com/api/orders/the-order-object |
| `order_refunded_partial.json`, `order_refunded_full.json` | `order_refunded` webhook (fires for full *and* partial refunds) | https://docs.lemonsqueezy.com/help/webhooks/event-types |
| `license_key_created.json` | `license_key_created` webhook (license key object) | https://docs.lemonsqueezy.com/api/license-keys/the-license-key-object |
| `refund_response.json` | response of `POST /v1/orders/:id/refund` (order object) | https://docs.lemonsqueezy.com/api/orders/issue-refund |

All money fields are integers in cents of the order currency (`total`, `tax`, `refunded_amount`, variant `price`).

Points the docs leave open. The code either makes them configurable or accepts them as missing:
- **Customer country:** the order object has no customer country. The adapter reads it from `meta.custom_data.country`
  (pass it at checkout), or from an explicit `country=` argument (for example looked up from the customer object).
  If neither is present, the ledger VAT line is booked without a country.
- **Withdrawal consent:** consent for EU digital content is not a Lemon Squeezy field. The adapter expects it in
  `meta.custom_data.withdrawal_consent_at` / `withdrawal_consent_version` when our checkout passes it.
- **`subtotal`:** the value used for tax-inclusive prices is a guess. The adapter only uses `total` and `tax`.
- **Default variant status:** a product's single default variant has status `pending`. The adapter treats both `pending`
  and `published` as sellable.
- **Licence key in fixtures:** the `key` value in `license_key_created.json` is a fake test value.
