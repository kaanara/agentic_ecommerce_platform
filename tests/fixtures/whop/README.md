# Whop fixtures (offline)

Hand-written from the Whop OpenAPI spec (https://api.whop.com/api/v1/openapi.json, fetched 2026-11-03) and the docs, not
captured from a live account. `tests/test_whop.py` and `tests/test_whop_ops.py` use them.

| File | Shape |
|---|---|
| `payment_succeeded.json` | webhook envelope `{id, type, api_version, account_id, data: Payment}` (money as `{amount: "49.00", currency, ...}`) |
| `payment_partially_refunded.json`, `payment_refunded.json` | `GET /payments/{id}` after a partial (12.10 incl. 2.10 tax) / full refund |
| `refund_created.json` | `refund.created` envelope; `data` is the Refund with a `payment` summary |
| `products.json`, `variants.json` | `GET /products`, `GET /variants` lists (`data`, `page_info`) |
| `openapi_operations.json` | method + path of the spec operations used by `store/whop_ops.py` |

Open points to check against the sandbox: whether `refunded_amount` is the gross (assumed, includes tax), whether Whop is
the merchant of record for EU VAT, and the exact `refund.*` payload.
