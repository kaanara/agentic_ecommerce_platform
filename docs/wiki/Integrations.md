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
- Inbound: point an AgentMail `message.received` webhook at `POST /webhooks/agentmail`. It publishes `email.received`, which the Support agent picks up.
- Admin commands (human use):
  ```bash
  uv run aecom agentmail keys              # list API keys
  uv run aecom agentmail create-key aecom-poc
  uv run aecom agentmail inboxes
  ```
> ⚠️ Use a **dedicated, least-privilege key** for this app, not an org-wide full-access PAT. Rotate any key that has been shared in chat, tickets or screenshots.

## Storefront (`StoreAdapter`)
`src/aecom/store/adapter.py` defines the protocol: products, prices, status, licence keys, inventory, orders, delivery, refunds. Agents only talk to the protocol, so switching platforms changes no prompts or graphs.

| Adapter | Status |
|---|---|
| `mock` | ✅ built-in, DB-backed, publishes `order.paid` / `inventory.*` events |
| Lemon Squeezy / Paddle / Shopify / Gumroad | ⏳ add `store/<platform>.py`, register it in `get_store()`, map platform webhooks to `order.paid` |

**VAT:** prices are VAT-inclusive. `vat_included(gross, country)` uses the customer's country rate (EU OSS). With a merchant-of-record platform the MoR files the VAT, but the ledger still tracks `vat_payable`.

## Ad platforms and social
Mocked today: `launch_campaign` sets the campaign live, and the simulator generates traffic and orders per channel profile. For a real channel, provision the account (see above) and approve that platform's MCP server for Marketing. Spend stays gated by `launch_campaign` limits and the monthly budget.

## Claude (Anthropic API)
`langchain-anthropic` `ChatAnthropic`, one model per tier, prompt caching on the persona prompt, and `max_retries=3`. The Researcher gets Anthropic's server-side **web search** (`server_tools: [web_search]`, tool type `WEB_SEARCH_TOOL_TYPE`).

## Observability
Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` in `.env` to trace every model and tool call. Business-level observability (runs, cost, decisions, audit) is in the Agents dashboard.
