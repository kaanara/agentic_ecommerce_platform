# Integrations

## Provisioning model
Agents **never** create accounts, accept terms, enter payment details or handle raw secrets. Instead the loop in `src/aecom/provisioning.py` runs:
1. **Need.** Any agent calls `request_account_need(vendor, why, evidence)` (T1). It's audited and published as `account.need_raised`.
2. **CEO decides.** A subscriber gives the CEO one task per vendor per day (dedupe setting `account_need:<vendor>:<date>`; a failed CEO run is retried by the bus). The CEO prefers existing vendors, records an ADR and calls `request_provisioning(vendor, steps, credential_env)`. Selection workflows can call it directly too.
3. **Human approves or rejects** (T3, Approvals). Approving marks the vendor `provisioning` and, on `approval.resolved`, stores its checklist `[{step, done}]` from the steps. Rejecting marks it `rejected` with the human's note and stores `Rejected <vendor>: <note>` in the CEO's memory (namespace `provisioning`), so it isn't requested again blindly; a later request for it reports that reason. A request for a vendor that is already `provisioning` or `active` changes nothing and says so.
4. **The human is told (E5).** When the approval executes, the app raises a P3 incident `provisioning_action` (subject = vendor, so it shows in the dashboard and digest) and emails `ALERT_EMAIL` an "action needed" message: vendor, purpose, monthly cost, the numbered steps, the env var **name** for the credential, and the steps that name scopes/permissions (grant the least access that works). No email without `ALERT_EMAIL`; it stays in the outbox unless `AGENTMAIL_LIVE=true`. A redelivered event sends nothing twice.
5. **Checklist and reminders.** While a vendor is `provisioning`, `check_credentials` emails a reminder 3 days after the approval and then at most every 3 days (`last_reminded_at`). The human works through the checklist on the CLI:
   ```bash
   uv run aecom provisioning                                  # vendors waiting, with their numbered checklist
   uv run aecom provisioning done "Acme Ads" 2 --by kaan      # tick step 2 (audited as human:kaan)
   uv run aecom provisioning snooze "Acme Ads" --days 7       # last_reminded_at = now + 7 days; next reminder 3 days after that
   ```
6. **Credential arrives and is verified (E6).** The human puts the credential in `.env` under `credential_env`. The scheduler's `provisioning` job (every 15 min, `check_credentials`) calls `verify(vendor)`, which picks a verifier from `VERIFIERS` by vendor name or category (normalised, e.g. `Lemon Squeezy` → `lemonsqueezy`). Each verifier makes **one read-only call** with a 10 s timeout:

   | Vendor | Call | Notes |
   |---|---|---|
   | `lemonsqueezy` | `GET https://api.lemonsqueezy.com/v1/users/me` | Bearer key |
   | `agentmail` | `GET /v0/inboxes/<AGENTMAIL_ORG_INBOX>` | then probes `GET /v0/api-keys`: if that succeeds the key is org-wide → warning "key has organization-wide access; create an inbox-scoped key" |
   | `anthropic` | `GET https://api.anthropic.com/v1/models` | `x-api-key` + `anthropic-version` |
   | anything else | none | presence only, with a warning that it wasn't verified |

   - **Failure** (non-200, timeout, network error): the vendor stays `provisioning`, the masked error goes to `last_check_error`, and the human gets one email per distinct error saying what's wrong. A missing credential is not an error email (the reminders cover it). Nothing raises out of `check_credentials`.
   - **Activation mode (U3).** `request_provisioning` takes `activation` (`auto` | `human_confirm` | empty) and `activation_reason`. The CEO proposes them; they are part of the approval args, so the human sees them on the approval card, can edit them, and approving the request approves the mode. Empty means: `auto` if a built-in verifier exists for the vendor, `human_confirm` for unknown vendors. Agents cannot activate a vendor.
   - **`human_confirm` success**: the vendor stays `provisioning` with `verified_at` set, the `provisioning_action` incident says it is waiting, ONE email asks the human to run the command below (not repeated on later checks) and the checklist gets the step "Confirm the vendor works (human)". `aecom provisioning list` shows the mode and `waiting_for_your_confirmation`.

     ```
     uv run aecom provisioning confirm "Acme Ads" --by kaan [--note "..."]   # audited as human:kaan
     ```

     `confirm` needs `--by`, requires the credential to be present and `verify()` to still pass (vendors without a verifier: presence only), then activates and does the usual hand-off below.
   - **`auto` success** (and after `confirm`): the vendor becomes `active` with `verified_at`; warnings are kept in `last_check_error` as `warning: ...` and emailed with the "is active" message; `vendor.provisioned` is published; the `provisioning_action` incident resolves; the Foreman gets the integration task (propose the MCP server or adapter; `STORE_ADAPTER=<platform>` for a store). If the Foreman is disabled or fails, a P3 incident `provisioning_followup` is raised instead.
   - The credential **value** is read only to make the call. It is never logged, audited, stored, emailed or put into events or prompts (only its env var name is); error text is passed through `observability.mask` and the value itself is scrubbed.

   To add a vendor: write `verify_<vendor>(vendor, credential) -> (ok, error, warnings)` in `src/aecom/provisioning.py` and register it in `VERIFIERS`.

Tools read credentials at call time via `SecretsProvider` (`src/aecom/secrets.py`). Today that's `.env` or the environment; it can be swapped for a vault.

## AgentMail (email)
- Client: `src/aecom/integrations/agentmail.py` (`https://api.agentmail.to/v0`).
- Org inbox: `AGENTMAIL_ORG_INBOX` (default `enrgy@agentmail.to`).
- **Outbox-only by default.** Every send is stored in `ops_outbox_emails`. Real delivery requires `AGENTMAIL_LIVE=true` **and** `AGENTMAIL_API_KEY`.
- Inbound, two ways, both publish `email.received` for the Support agent:
  - **Polling** (works without a public URL): the scheduler runs `poll_inbox()` every 60 s when a key is set, or run `uv run aecom agentmail poll`. It stores each new unread customer email with its event in one commit, then marks it read in AgentMail. Re-polls never create duplicates.
  - **Webhook**: point an AgentMail `message.received` webhook at `POST /webhooks/agentmail` (see [Webhooks](#webhooks)).
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

### Download links
With `STORE_ADAPTER=lemonsqueezy`, Lemon Squeezy hosts and delivers the files. The `mock` store issues one link per order on first delivery: `{DOWNLOAD_BASE_URL}/{order_id}/{token}` (default `https://downloads.example`). It is valid for `DOWNLOAD_LINK_DAYS` (default 7). Resending the access email reuses the same link while it is valid. Once the link expires, the next delivery issues a new token and writes the audit entry `download_link_reissued`; the token is never logged. `MockStore.verify_download(order_id, token)` is what a file server should call. It compares in constant time and rejects expired links and fully refunded orders. Partially refunded orders keep access. The link never shows `Product.asset_url`, which is the file location used by the server that serves the downloads.

### Lemon Squeezy (merchant of record)
**What Lemon Squeezy does:** checkout, payment, EU VAT (charged and filed as merchant of record), licence key generation, download hosting, receipt and access emails, and refunds. Sales keep running when the app host (Pi) is off, and Lemon Squeezy retries webhooks.

**What the app mirrors:** each order becomes a local customer, an order (`external_id` = Lemon Squeezy order id) and ledger lines, so agents, monitors, reports and the chaos invariants work unchanged.
- `mirror_order(payload)` handles `order_created` with status `paid`. In one transaction it writes the customer, an order with `amount_eur` = `total` and `vat_eur` = Lemon Squeezy's `tax` (never recomputed), `ledger.record_sale` with that tax and rate, and any withdrawal consent the checkout passed. The order is marked `delivered` with `email_sent` set, because Lemon Squeezy has already delivered and emailed. The function publishes `order.mirrored`, **not** `order.paid`, so nothing is delivered twice. It is idempotent on the order id.
- `mirror_refund(payload)` handles `order_refunded`, which fires for full and partial refunds. Lemon Squeezy's cumulative `refunded_amount` is authoritative, and only the part not yet mirrored is booked. The VAT reversal is pro rata on Lemon Squeezy's tax, so a full refund reverses exactly the booked VAT. The status becomes `partially_refunded` or `refunded`, and a full refund revokes the key. It is idempotent and safe out of order: a refund for an unknown order mirrors the sale first.
- `mirror_license_key(payload)` handles `license_key_created` / `license_key_updated`. It stores only `ls:<key id>:****<last4>`; the full key is never stored or logged. While the order is not mirrored yet it raises `HandlerFailed` (retry).
- `dispatch(payload)` routes a webhook body by `meta.event_name`. `verify_signature(raw_body, X-Signature)` checks the HMAC-SHA256 against `LEMONSQUEEZY_WEBHOOK_SECRET`. The HTTP route is `POST /webhooks/lemonsqueezy` (see [Webhooks](#webhooks)).

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

## Webhooks
`src/aecom/api/webhooks.py`. These routes are **not** behind the API bearer token, because providers can't send it. Instead each one verifies the provider's signature over the raw body and **fails closed**: no secret configured → 503, bad or missing signature → 401, and nothing is written. Every delivery is recorded once in `ops_webhook_receipts` (unique `provider, event_key`), so a provider's retry or duplicate returns 200 `{"duplicate": true}` without side effects.

> ⚠️ **Expose only `/webhooks/*`.** Run the API on the host (Pi) bound to localhost and publish just these routes through a tunnel (e.g. a Cloudflare Tunnel or ngrok path rule to `http://127.0.0.1:8000/webhooks/`). The dashboard and every other API route must never be reachable from the internet.

### Lemon Squeezy: `POST /webhooks/lemonsqueezy`
- **Configure:** a human creates the webhook in the Lemon Squeezy dashboard (Settings > Webhooks) with the tunnel URL, a signing secret and the events `order_created, order_refunded, license_key_created, license_key_updated`. The secret goes into `.env` as `LEMONSQUEEZY_WEBHOOK_SECRET`.
- **Signature:** `X-Signature` must equal the hex HMAC-SHA256 of the raw body under the secret (constant-time compare; [docs](https://docs.lemonsqueezy.com/help/webhooks/signing-requests)).
- **Routing** by `meta.event_name` (or the `X-Event-Name` header): `order_created` (paid) → `mirror_order`, `order_refunded` → `mirror_refund`, `license_key_created` / `license_key_updated` → `mirror_license_key`. Other events are acknowledged with 200 and recorded as ignored.
- **Idempotency:** the receipt key is `meta.event_id` if present, otherwise the SHA-256 of the raw body (Lemon Squeezy retries send the identical body). The mirrors are idempotent on the Lemon Squeezy ids too.
- **Errors:** if mirroring fails, the error is stored (`record_error`, component `webhook.lemonsqueezy`), the receipt is removed again and the route returns 500, so Lemon Squeezy retries. A licence key that arrives before its order is retried this way.

### AgentMail: `POST /webhooks/agentmail`
- **Configure:** a human creates a `message.received` webhook in the AgentMail console with the tunnel URL and copies its `whsec_...` secret into `.env` as `AGENTMAIL_WEBHOOK_SECRET`.
- **Signature** (Svix scheme, [docs](https://docs.agentmail.to/webhook-verification)): headers `svix-id`, `svix-timestamp`, `svix-signature`. The signature is the base64 HMAC-SHA256 of `{svix-id}.{svix-timestamp}.{raw body}`, keyed with the base64-decoded part after `whsec_`. Any `v1,<sig>` entry in the space-separated header may match (constant-time compare). Timestamps more than 5 minutes from the server clock are rejected (replay protection), so keep the host clock in sync (NTP).
- **Idempotency:** on `svix-id` (Svix retries reuse it). The receipt and the `email.received` event (`from, subject, body, message_id, thread_id, labels`) commit in one transaction. Other `event_type`s are acknowledged and recorded as ignored.
- Polling (`poll_inbox`) still works without a public URL. It deduplicates on its own.

## Ad platforms and social
Mocked today: `launch_campaign` sets the campaign live, and the simulator generates traffic and orders per channel profile. For a real channel, provision the account (see above) and approve that platform's MCP server for Marketing. Spend stays gated by `launch_campaign` limits and the monthly budget.

## Claude (Anthropic API)
`langchain-anthropic` `ChatAnthropic`, one model per tier, prompt caching on the persona prompt, and `max_retries=3`. The Researcher gets Anthropic's server-side **web search** (`server_tools: [web_search]`, tool type `WEB_SEARCH_TOOL_TYPE`).

## Observability
Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` in `.env` to trace every model and tool call. Business-level observability (runs, cost, decisions, audit) is in the Agents dashboard.
