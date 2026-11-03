# Polar fixtures (offline)

Written by hand from the Polar docs (read 2026-11-03), not captured from a live account. `tests/test_polar.py` uses them.

| File | Shape | Source |
|---|---|---|
| `products.json` | `GET /v1/products/` list (`items`, `pagination.max_page`) | https://polar.sh/docs/api-reference/products/list |
| `order_paid.json` | webhook body `{type, timestamp, data: Order}` | https://polar.sh/docs/integrate/webhooks/events |
| `order_refunded_partial.json`, `order_refunded_full.json` | `order.refunded` with cumulative `refunded_amount` + `refunded_tax_amount` | same |
| `refund_response.json` | response of `POST /v1/refunds/` | https://polar.sh/docs/api-reference/refunds/create |

Amounts are integer cents. Open points to check against the sandbox: whether `refunded_amount` excludes tax (the
code assumes it does and adds `refunded_tax_amount`), whether `order.refunded` fires for partial refunds (the
reconciler covers it if not), and the refund request's `amount` being the net part.
