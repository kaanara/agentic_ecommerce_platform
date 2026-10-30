# Stripe fixtures (offline)

These JSON files were written by hand to match the shapes in the Stripe API docs (read on 2026-10-30).
`tests/test_payouts.py` uses them to test `aecom.payouts.fetch_stripe`'s *parsing*, so no test calls the real API.
**The live call itself (the actual network request/response round trip against `api.stripe.com`) is untested** -
see the "Stripe fetch is untested live" note in `docs/wiki/Payouts.md`.

| File | Shape | Source |
|---|---|---|
| `payouts_list.json` | `GET /v1/payouts` (list of payout objects) | https://docs.stripe.com/api/payouts/list, https://docs.stripe.com/api/payouts/object |
| `balance_transactions_po_1PAYOUTfixture01.json` | `GET /v1/balance_transactions?payout=<id>` (the balance transactions settled by that payout - Stripe's own documented reconciliation pattern) | https://docs.stripe.com/api/balance_transactions/list, https://docs.stripe.com/payouts/reconciliation |

All money fields (`amount`, `fee`, `net`) are integers in cents of the transaction currency. The fixture totals
are internally consistent: two `charge` transactions (gross 6000 + 4000 = 10000 cents, fees 180 + 120 = 300 cents)
and one `refund` (-500 cents, fee 0) net to 5820 + 3880 - 500 = 9200 cents, which is the fixture payout's `amount`
- the same arithmetic `aecom.payouts._stripe_record` does to build an `ingest` record (gross 100.00 EUR, fees
3.00 EUR, refunds 5.00 EUR, net 92.00 EUR).
