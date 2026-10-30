# Payouts

F4 in the [Roadmap](Roadmap), and the remaining part of R13 ("book Lemon Squeezy fees as expenses"). Implemented
in `aecom/payouts.py`, the `payout_unmatched` monitor in `aecom/monitors.py`, and the dashboard tab
`dashboard/_acct_payouts.py` (Govern > Accounting > Payouts).

Platforms (Lemon Squeezy, Stripe, PayPal, ...) pay out to the bank **net of fees**, on their own schedule - rarely
1:1 with individual orders. This module closes that gap: a payout is ingested (one record, from a CSV export or
the optional Stripe API), then **reconciled** against the orders this app mirrored and the refunds booked for
them inside the payout's covered period:

```
expected net = gross orders in period - refunds in period - platform fees
```

If that matches the payout's `net_eur` (what actually reached the bank) to the cent, it is `matched`; otherwise
`mismatch`, with a signed `difference_eur` (expected minus actual) for a human to investigate. Either way, on the
**first** reconciliation the platform's fees are booked once, as a balanced ledger entry:

```
debit  vendor_expense   <fees>
credit cash             <fees>
memo:  "platform fees <provider> <external_id>"
ref:   <payout id>
```

so the fees show up in the P&L (R13). This never double-books (`fee_txn` on the row remembers the ledger
transaction) and refuses to post into a **closed month**: `aecom.periods` raises `PeriodClosed` (a `ValueError`
with a clear message), which propagates straight out of `reconcile` and leaves the payout - and everything else
about it - exactly as it was. Nothing partial is ever committed.

## Scope: which orders are "covered"

Covered orders today are **Lemon Squeezy mirrors only** (`Order.external_id` set) - the only storefront this app
integrates with (see `aecom.store.lemonsqueezy`). A payout ingested for another provider (Stripe, PayPal) still
reconciles, it just finds no local orders to match against - a mismatch, unless its fees also happen to be zero -
until that provider's own orders are mirrored here too (F3, a second storefront channel). This is a deliberate,
honest limitation, not a bug: there is nothing to reconcile against for a provider whose sales this app has never
seen.

## API

```python
from aecom import payouts

payouts.ingest(provider, record, by)             # one payout, idempotent on (provider, external_id)
payouts.import_csv(provider, text, mapping, by)   # a payout export (any platform's CSV), flexible columns
payouts.reconcile(payout_id, by)                  # match it and (once) book its fees
payouts.list_payouts(status=None)                 # all payouts, optionally filtered
payouts.unmatched()                                # pending or mismatch - what needs attention
payouts.fetch_stripe(since, by)                   # optional: Stripe API, via net_guard
```

### `ingest(provider, record, by)`
`provider` is one of `payouts.LEMONSQUEEZY`, `payouts.STRIPE`, `payouts.PAYPAL`. `record` carries `external_id`
(required) and any of `period_start`/`period_end` (date, datetime or ISO string), `currency` (default `EUR`),
`gross_eur`/`fees_eur`/`refunds_eur`/`net_eur` (numbers or CSV-shaped strings such as `"1,234.56"` or `"(12.00)"`,
default 0) and `note`. **Idempotent** on `(provider, external_id)`: ingesting the same payout again returns the
existing row unchanged (`already: True`) rather than silently overwriting it - re-import a corrected export only
before the payout has reconciled, or fix the stored numbers directly.

### `import_csv(provider, text, mapping=None, by="")`
Any platform's payout CSV. Common export headers (Lemon Squeezy, Stripe, PayPal) are recognised automatically -
see `payouts.HEADER_ALIASES` for the exact alias lists. Pass `mapping={"external_id": "Payout Reference", ...}`
to name a column explicitly when a platform's export uses something else. Every parsed row goes through `ingest`
(so re-importing the same file is safe). Errors are plain and name the **row** (1-based; the header is row 1, so
the first data row is row 2) and **column**, e.g.:

```
row 3, column 'Gross': 'n/a' is not a number
```

### `reconcile(payout_id, by="")`
Safe to call again later (e.g. once more orders have been mirrored, or a mismatch was investigated and the
payout's stored numbers were corrected): it recomputes the match every time, but only ever books the fees once.

### `fetch_stripe(since, by="system")` - optional, **live call untested**
Fetches paid Stripe payouts created on/after `since` via the Stripe API (`GET /v1/payouts`, then
`GET /v1/balance_transactions?payout=<id>` for each one - Stripe's own documented reconciliation pattern,
https://docs.stripe.com/payouts/reconciliation) and ingests each one. Requires `STRIPE_API_KEY` (read through
`aecom.secrets`, same pattern as every other provider key); raises `RuntimeError` if it is not set. Goes through
`aecom.net_guard` (`resolve_public` + `pinned_get`), the same IP-pinned, SSRF-guarded GET every other outbound
call in this app uses - never plain `httpx`.

> **Honesty notice:** only the JSON *parsing* (`aecom.payouts._stripe_record`) is tested, against the recorded
> fixtures in `tests/fixtures/stripe/` (monkeypatched `resolve_public`/`pinned_get`, the same technique
> `tests/test_secret_ping.py` uses). **The live call itself - the real network round trip against
> `api.stripe.com` - has never been run in this session.** Treat `fetch_stripe` as unverified against the real
> API until someone runs it with a real `STRIPE_API_KEY` and checks the result.

Only EUR payouts are supported; a non-EUR Stripe payout raises `ValueError` rather than silently converting it.

## Month-end report

`aecom.month_end`'s `payout_reconciliation` section counts payouts **received in the month** (`created_at`, the
same ingestion-time convention every other section of that report uses): total count, gross/fees/refunds/net,
how many are matched/mismatch/pending, and the open (not-yet-matched) ones by id. See `docs/wiki/Reporting.md`.

## The `payout_unmatched` monitor (P3)

A payout still `pending` (never reconciled) or `mismatch` (reconciled, but the numbers don't add up) for more
than 14 days raises a P3 incident - not urgent enough to page anyone, but a reminder that it is waiting on a
human. It auto-resolves once the payout is reconciled to `matched` (or investigated and fixed).

## Dashboard: Govern > Accounting > Payouts

Upload a payout CSV, pick the provider, preview the parsed rows, then import. The payouts table shows every
ingested payout with a status badge and its difference (if mismatched); each non-matched row gets its own
"Reconcile" button, plus a "Reconcile all pending" button for the whole backlog. Errors from a bad CSV or a
closed-month refusal show up as a plain, specific message - never a stack trace.

## What a human still has to do

- Export the payout CSV from the platform's dashboard (Lemon Squeezy, Stripe, PayPal, ...) and upload it, or set
  `STRIPE_API_KEY` and call `payouts.fetch_stripe` (CLI patch pending, see below) - **unverified against the
  real Stripe API**, see the honesty notice above.
- Investigate and resolve a `mismatch` (a human decision: was a fee misreported, an order missed, a refund not
  yet mirrored?). `reconcile` only ever reports the mismatch; it never guesses a correction.
- Review a payout from a provider this app does not yet mirror (Stripe, PayPal) - it will always mismatch today
  (see "Scope" above) until that provider's own orders are mirrored here (F3).

## Known gaps / follow-ups for the lead

- **CLI**: no `aecom payouts list|import|reconcile` subcommand yet (`cli.py` is not an owned file for this
  change). The dashboard tab and the Python API (`aecom.payouts`) are both complete; wiring a CLI subcommand is a
  small, mechanical addition following the existing `period`/`month-end` command pattern in `cli.py`.
- **Settings registry**: `STRIPE_API_KEY` is read directly via `aecom.secrets` (like `MIGRATION_REVENUE_EUR`), so
  it is not yet a field in `settings_registry.py` / the Settings dashboard page - it can only be set by editing
  `.env` directly today. Worth adding for discoverability once Stripe is actually in use.
- **Roadmap.md**: F4 and the remaining part of R13 are now built; the lead should update their rows in
  `docs/wiki/Roadmap.md` (not an owned file for this change).
