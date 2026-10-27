# Integrations

## Provisioning model
Agents **never** create accounts, accept terms, enter payment details or handle raw secrets. Instead:
1. The selection workflow picks a vendor, and the CEO records an ADR.
2. The CEO calls `request_provisioning(vendor, steps, credential_env)`. That's T3, so it lands in Approvals.
3. A human approves, which marks the vendor `provisioning`, then follows the steps and puts the credential in `.env` under `credential_env`.
4. The adapter or MCP server is activated: `STORE_ADAPTER=<platform>`, or the Foreman proposes the vendor's MCP server, which a human promotes and grants.

Tools read credentials at call time via `SecretsProvider` (`src/aecom/secrets.py`). Today that's `.env` or the environment; it can be swapped for a vault.

## AgentMail (email)
- Client: `src/aecom/integrations/agentmail.py` (`https://api.agentmail.to/v0`).
- Org inbox: `AGENTMAIL_ORG_INBOX` (default `enrgy@agentmail.to`).
- **Outbox-only by default.** Every send is stored in `ops_outbox_emails`. Real delivery requires `AGENTMAIL_LIVE=true` **and** `AGENTMAIL_API_KEY`.
- Inbound, two ways, both publish `email.received` for the Support agent:
  - **Polling** (works without a public URL): the scheduler runs `poll_inbox()` every 60 s when a key is set, or run `uv run aecom agentmail poll`. It stores each new unread customer email with its event in one commit, then marks it read in AgentMail. Re-polls never create duplicates.
  - **Webhook**: point an AgentMail `message.received` webhook at `POST /webhooks/agentmail` (signature check still to do).
- Reading needs only a key; `AGENTMAIL_LIVE` controls sending.
- **Recipient restriction:** Support serves the sender of the email it is handling. `send_email` to anyone else escalates; `reply_email` answers in the customer's thread and refuses other customers' emails.
- **Key:** use an inbox-scoped key with `inbox_read, message_read, message_send, message_update` (`uv run python scripts/agentmail_inbox_key.py`). The app's admin commands then return 403 by design.
- Admin commands (human use):
  ```bash
  uv run aecom agentmail keys              # list API keys
  uv run aecom agentmail create-key aecom-poc
  uv run aecom agentmail inboxes
  ```
> ⚠️ Use a **dedicated, least-privilege key** for this app, not an org-wide full-access PAT. Rotate any key that has been shared in chat, tickets or screenshots.

## Storefront (`StoreAdapter`)
`src/aecom/store/adapter.py` defines the protocol: products, prices, status, licence keys, inventory, orders, delivery and refunds. Agents only use the protocol, so switching platforms changes no prompts or graphs.

| Adapter | Status |
|---|---|
| `mock` | ✅ Built in and DB-backed. Publishes `order.paid` / `inventory.*` events, and the app delivers and emails. |
| `lemonsqueezy` | ✅ `src/aecom/store/lemonsqueezy.py`. Lemon Squeezy is the system of record (Roadmap L1). The app mirrors orders, refunds and licence keys. |
| Paddle / Shopify / Gumroad | ⏳ Add `store/<platform>.py` and register it in `get_store()`. |

### Lemon Squeezy (merchant of record)
**What Lemon Squeezy does:** checkout, payment, EU VAT (charged and filed as merchant of record), licence key generation, download hosting, receipt and access emails, and refunds. Sales keep running when the app host (Pi) is off, and Lemon Squeezy retries webhooks.

**What the app mirrors:** each order becomes a local customer, an order (`external_id` = Lemon Squeezy order id) and ledger lines, so agents, monitors, reports and the chaos invariants work unchanged.
- `mirror_order(payload)` handles `order_created` with status `paid`. In one transaction it writes the customer, an order with `amount_eur` = `total` and `vat_eur` = Lemon Squeezy's `tax` (never recomputed), `ledger.record_sale` with that tax and rate, and any withdrawal consent the checkout passed. The order is marked `delivered` with `email_sent` set, because Lemon Squeezy has already delivered and emailed. The function publishes `order.mirrored`, **not** `order.paid`, so nothing is delivered twice. It is idempotent on the order id.
- `mirror_refund(payload)` handles `order_refunded`, which fires for full and partial refunds. Lemon Squeezy's cumulative `refunded_amount` is authoritative, and only the part not yet mirrored is booked. The VAT reversal is pro rata on Lemon Squeezy's tax, so a full refund reverses exactly the booked VAT. The status becomes `partially_refunded` or `refunded`, and a full refund revokes the key. It is idempotent and safe out of order: a refund for an unknown order mirrors the sale first.
- `mirror_license_key(payload)` handles `license_key_created` / `license_key_updated`. It stores only `ls:<key id>:****<last4>`; the full key is never stored or logged. While the order is not mirrored yet it raises `HandlerFailed` (retry).
- `dispatch(payload)` routes a webhook body by `meta.event_name`. `verify_signature(raw_body, X-Signature)` checks the HMAC-SHA256 against `LEMONSQUEEZY_WEBHOOK_SECRET`.

**Adapter behaviour:**
- **Catalog:** `sync_catalog()` reads products and variants over the REST API (`https://api.lemonsqueezy.com/v1`, JSON:API, Bearer key). It upserts one local product per variant (`external_id` = variant id, new SKU `LS-<variant id>`). Local SKUs, unit costs and vendors are kept.
- **Reads:** list, get, inventory and order reads come from the local mirror. `available_keys` is `None`, because Lemon Squeezy generates a key per purchase.
- **Writes:** `create_product`, `update_price`, `set_status`, `add_license_keys` and `create_order` raise `NotImplementedError`, because products and prices live in the Lemon Squeezy dashboard. The agent gets an ERROR result and asks a human, who changes the product in the dashboard and re-syncs.
- **Delivery:** `deliver_order` is a no-op (`delivered_by: lemonsqueezy`).
- **Refunds:** `refund_order` calls `POST /v1/orders/:id/refund` (amount in cents; the API supports partial refunds) and mirrors the returned order. The later webhook changes nothing.

**Switching:**
1. A human creates the store, products and variants in the dashboard (enable licence keys on licence variants), plus an API key and a webhook for `order_created, order_refunded, license_key_created, license_key_updated`. Use the provisioning flow above; agents never sign up.
2. Set `STORE_ADAPTER=lemonsqueezy`, `LEMONSQUEEZY_API_KEY`, `LEMONSQUEEZY_STORE_ID` and `LEMONSQUEEZY_WEBHOOK_SECRET` in `.env`, then run `sync_catalog()`.
3. In the checkout, pass `custom_data` with `country` (ISO alpha-2). The order object has no customer country, and without it the ledger VAT line has no country. Optionally also pass `withdrawal_consent_at`, `withdrawal_consent_version` and `campaign_id`.
4. The ledger books the store currency (`LEMONSQUEEZY_CURRENCY`, default EUR). Orders in another currency are refused, not converted.

The simulator and `create_order` scenarios need `STORE_ADAPTER=mock`. Payload shapes and their doc sources are listed in `tests/fixtures/lemonsqueezy/README.md`.

**VAT:** prices are VAT-inclusive. With `mock`, `vat_included(gross, country)` uses the customer's country rate (EU OSS). With a merchant of record, the MoR charges and files the VAT. The ledger still tracks `vat_payable` with the MoR's own amounts, per country and rate.

## Ad platforms and social
Mocked today: `launch_campaign` sets the campaign live, and the simulator generates traffic and orders per channel profile. For a real channel, provision the account (see above) and approve that platform's MCP server for Marketing. Spend stays gated by `launch_campaign` limits and the monthly budget.

## Claude (Anthropic API)
`langchain-anthropic` `ChatAnthropic`, one model per tier, prompt caching on the persona prompt, and `max_retries=3`. The Researcher gets Anthropic's server-side **web search** (`server_tools: [web_search]`, tool type `WEB_SEARCH_TOOL_TYPE`).

## Observability
Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` in `.env` to trace every model and tool call. Business-level observability (runs, cost, decisions, audit) is in the Agents dashboard.
