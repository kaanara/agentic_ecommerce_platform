# Roadmap

## PoC: done
- 9 agents with SOUL / IDENTITY / AGENTS / TOOLS / POLICY files
- Company and selection graphs (LangGraph, checkpointed)
- Policy gate (T0–T4), HITL approvals with edit-before-approve, kill switch, budgets, append-only audit
- Tool / skill / MCP registry with a governed lifecycle; company MCP servers
- Mock store, double-entry ledger with EU VAT, AgentMail integration (outbox by default)
- Event bus, reflexes, scheduler, market simulator
- Business / Agents / Approvals dashboards; 8 scenarios with offline replay; test suite

## Production readiness backlog
Target setup: runs 24/7 on a Raspberry Pi 4 (64-bit OS), dashboards opened from another computer, digital products only, human in the loop at first and gradually autonomous. The items are scheduled into phases, with exit criteria, in the [Development Plan](Development-Plan).

### A. Reliability: don't lose sales
| # | Item | Notes |
|---|---|---|
| A1 ✅ 2026-10-25 | Event retries + dead letter | `bus/events.py` marks events processed before the handler runs, so a failed `deliver_order` is lost. Switch to at-least-once with retry and dead letter |
| A2 | Order reconciler job | Every 15 min: paid but undelivered orders → deliver, alert if it still fails |
| A3 ✅ 2026-10-25 | Fix stuck `running` runs | The `except` path in `graph/agents.py` must set the `ops_agent_runs` row to `error` |
| A4 | Split the kill switch | *Pause agents* vs. *stop selling*. Today the kill switch also stops deliveries |
| A5 | Lemon Squeezy as store (MoR) | Checkout, payment, VAT and key delivery keep working when the Pi is off; webhooks retry |

### B. Monitoring and alerting
See [Monitoring and Alerting](Monitoring-and-Alerting): deterministic monitors, P1/P2/P3 routing (push + email + digest), external heartbeat, `ops_incidents` + Health page, a real `/health`, then a read-only Sentinel agent for diagnosis.

### C. Running on the Pi
| # | Item | Notes |
|---|---|---|
| C1 | systemd units | `aecom-dashboard`, `aecom-scheduler`, `Restart=always` |
| C2 | `scripts/update.sh` | Kill switch on → stop → back up `data/` → `git pull` → `uv sync` → `aecom init` (no `--demo`) → start → kill switch off |
| C3 | Off-Pi backups | Daily copy of `data/` (both `aecom.db` and `aecom.checkpoints.db`) |
| C4 | Alembic migrations | `create_all` never alters existing tables |
| C5 | Re-sync `TOOLS.md` | `seed_registry` ignores tool changes for agents that are already seeded |
| C6 | Dashboard login | No authentication today; anyone on the LAN can approve. Remote access via Tailscale or an SSH tunnel only |

### D. Monitoring agents
| # | Item | Notes |
|---|---|---|
| D1 | Live panel | Agents running now, auto-refresh |
| D2 | Run detail page | One `run_id` → agents, tool calls, approvals, decision on one timeline |
| D3 | Tool-call arguments and results | Store a shortened copy in `ops_tool_calls` |
| D4 | Web search cost | Server-tool search fees are missing from `cost_eur`; `max_uses` applies per API call, not per run |

### E. Closing the CEO's decision chain
| # | Item | Notes |
|---|---|---|
| E1 | Provisioning loop | Subscribe to `approval.resolved` (it has no subscriber today); detect when `credential_env` appears; mark the vendor `active`; send the Foreman an integration task |
| E2 | Vendor MCP first | Foreman proposes and sandboxes the vendor's MCP server; read-only tools granted first |
| E3 | Engineer agent | Writes adapters and webhooks on a branch and opens a PR; a human reviews and merges; never deploys |
| E4 | Any agent can raise an account need | New tool `request_account_need(vendor, why, evidence)` for all agents → goes to the CEO, who decides and calls `request_provisioning`. Today only the CEO can ask, so other agents can only mention a need in their output |
| E5 | Provisioning task with notification | Status `requested → approved → in_progress → active / rejected`; checklist of the steps; "action needed" message by email or push (B routing) with purpose, cost, steps, the credential name and required scopes; reminder after 3 days; snooze |
| E6 | Credential verification | When `credential_env` appears: one read-only test call, and a scope check where the platform allows it (warn if broader than requested). Success → vendor `active`, CEO informed, Foreman integration task (E1). Failure → message to the human saying what's wrong |
| E7 | Feedback to the CEO | Rejections and their reasons go back to the CEO and are recorded, so the same request isn't repeated; duplicate open requests per vendor are merged |

### F. Platforms and credentials
| # | Item | Notes |
|---|---|---|
| F1 | Credentials | Logins stay with humans in a password manager with 2FA; the app holds only narrowly scoped API keys. `chmod 600 .env`; later a vault behind `SecretsProvider` |
| F2 | OAuth token store | Refresh tokens need writable storage; `.env` is read once at startup |
| F3 | Channels | `StoreAdapter` assumes one store. A second channel (Etsy) needs listing sync and one shared key pool |
| F4 | Payout reconciliation | Stripe / PayPal payouts and fees into the ledger |
| F5 | Policy rules for MCP tools | e.g. `mcp:stripe/create_refund` with limits; unclassified tools default to T3 |
| F6 | Platform skills | Etsy listing rules, Shopify SEO, EU VAT/OSS checklist |
| — | Out of scope while selling digital goods only | Shipping (ShipStation, Sendcloud, Shippo), Amazon, eBay, Kaufland/Otto, Square, Adyen |

### G. Chaos testing and gradual autonomy
| # | Item | Notes |
|---|---|---|
| G1 | Invariants | Every paid order delivered; ledger balances; no key on two orders; spend ≤ budget; no side effects with the kill switch on (except delivery); no run stuck `running` |
| G2 | Fault injectors | Delivery crash, duplicate webhook, lost webhook, Claude API down, DB locked, vendor outage, injection variants |
| G3 | `aecom chaos` | Offline (scripted, CI) and live (real Claude); seeded "random bad week" |
| G4 | Trust report | Per action: escalations, approve-unchanged rate, a "promote" button |
| G5 | Autonomy stages | Per-action stages in `policy.yaml`, stage stored in `ops_settings` (set by humans only); allow + notify for stage 3 |
| G6 | Automatic demotion | Tighten limits on KPI drops, Judge scores or incidents |
| — | Always human | Account creation, terms, payment methods, budget increases, `policy.yaml` and code changes |

### H. Engineering hygiene, security and compliance
| # | Item | Notes |
|---|---|---|
| H1 ✅ 2026-10-26 | CI | No `.github/` workflows yet. Run tests and offline scenarios on every push |
| H2 | Staging | Laptop in `LLM_MODE=scripted`; the Pi runs `main` only |
| H3 | Rotate the AgentMail key | Revoke the shared org PAT; use the dedicated `aecom-poc` key |
| H4 | Anthropic Console spend limit | Hard cap outside the app |
| H5 ✅ 2026-10-26 | SSRF guard in `fetch_url` | Only checks for `http(s)://`; block loopback, private and link-local addresses |
| H6 | Host hygiene | Log rotation, NTP, SD-card health or USB SSD |
| H7 | Legal pages | Legal notice (Impressum), privacy policy, terms |
| H8 | Withdrawal consent | EU digital content: consent to immediate delivery and loss of the 14-day withdrawal right, recorded per order |
| H9 | GDPR requests | Human procedure for access and deletion; `delete_customer_data` stays T4 |
| H10 | Data retention | Orders, emails, audit logs, LangSmith traces |
| H11 | LangSmith | Tracing on, PII handled per H10 |

### I. Code review findings (2026-10-25)
Confirmed by reading the code. The offline scenarios don't catch them because the simulator only exercises happy paths.

| # | Sev | Finding | Where | Fix |
|---|---|---|---|---|
| I1 | ✅ done 2026-10-26 | **API has no authentication.** Anyone who can reach it can approve (with any `by` name), toggle the kill switch, start runs, and publish arbitrary events, including `email.received` (a free prompt-injection channel) | `api/app.py` | Token auth on every route; expose only signed webhook routes through the tunnel |
| I2 | ✅ done 2026-10-25 | **Support can email anyone.** `send_email` (T1) accepts any recipient, and Support also has `list_orders` / `get_order` with customer emails. An injected email can make it send other customers' data to an attacker | `tools/catalog.py`, `agents/support/TOOLS.md`, `policy.yaml` | Restrict recipients to the customer of the current thread or order; escalate anything else |
| I3 | ✅ done 2026-10-26 | **Approvals bypass the kill switch.** `approve()` runs the parked call directly, so approving during an emergency still executes it | `approvals.py` | Re-check the kill switch (and only the kill switch) before executing |
| I4 | 🔴 | **Key never reaches the customer if the email fails.** `deliver_order` commits `delivered` and *then* sends the email. If AgentMail fails, a retry returns `already` and never resends | `tools/catalog.py`, `store/mock.py` | Track `email_sent` separately; resend on retry; invariant "every delivered order has a sent email" |
| I5 | 🔴 | **Restock mints fake licence keys.** The `inventory.low` / `out_of_stock` reflexes call `add_license_keys(count=50)`, which generates random strings. With a real store, customers would get keys that don't work | `handlers.py`, `tools/catalog.py` | Restock from the vendor's key feed or the store platform only; with no feed → alert, never mint |
| I6 | 🟠 | **Refunds don't reverse VAT.** `record_refund` debits the gross amount from `refunds` but leaves `vat_payable` unchanged, so net revenue is understated and VAT is overpaid | `ledger.py` | Split the refund into net + VAT; reverse `vat_payable` |
| I7 | 🟠 | **Partial refund behaves like a full one.** Any amount marks the order `refunded` and revokes the key, and the remainder can never be refunded | `store/mock.py` | Track refunded amount; revoke only on a full refund |
| I8 | 🟠 | **Missing EU VAT rates default to 0%.** `VAT_RATES` has 10 countries; BE, PT, IE, DK, FI, CZ, GR, HU, RO and others get no VAT | `store/adapter.py` | All 27 EU rates, plus an error for unknown countries instead of 0% |
| I9 | 🟠 | **Monthly marketing budget can be bypassed.** The spend check counts only `live` and `ended` campaigns created this month. Pausing a campaign removes it from the total; campaigns from last month are never counted | `policy/gate.py` | Count actual spend (`spent_eur`) of all campaigns active this month, whatever their status |
| I10 | 🟠 | **Duplicate approvals.** The same escalated call can be parked twice, and approving both executes it twice (e.g. two campaign launches). Approvals also never expire | `policy/gate.py`, `approvals.py` | Deduplicate pending approvals by tool + args; expire after N days |
| I11 | 🟠 | **Order, ledger and event aren't one transaction.** `create_order` commits the order, then posts to the ledger, then publishes `order.paid`, each separately. A crash in between leaves an order with no ledger entry or no delivery | `store/mock.py` | Transactional outbox: write the order, ledger lines and event in one commit |
| I12 | 🟡 | **Money stored as floats.** Rounding drift across many orders breaks the "ledger balances" invariant | `ledger.py`, `db.py` | Integer cents or `Decimal` |
| I13 | 🟡 | **Download links are fake.** `deliver_order` returns a random `downloads.example` URL; there is no file hosting, no signed URL, no expiry or resend | `store/mock.py` | Hosted assets with signed, expiring URLs (the store platform or object storage) |
| I14 | 🟡 | **Newsletter ignores consent.** `send_newsletter` emails every customer; "reply STOP" isn't processed; no marketing consent is recorded | `tools/catalog.py`, `db.py` | Consent flag per customer, unsubscribe handling, suppression list |
| I15 | 🟡 | **Shared memory can be poisoned.** Any agent can `remember(shared=True)` at T1, including right after reading an untrusted email; every agent then recalls it | `tools/catalog.py`, `policy.yaml` | Store the source with each memory; shared memories need Judge or human review |
| I16 | 🟡 | **Secrets in plain text in the DB and dashboard.** Licence keys and download links are stored in `ops_outbox_emails` and in the audit log args, and both are shown on the dashboard | `integrations/agentmail.py`, `policy/gate.py` | Mask secrets in the audit log and dashboard views |
| — | ℹ️ | On Postgres, two concurrent deliveries can pick the same key (`.first()` without a row lock; SQLite serialises writers, so not today) | `store/mock.py` | `SELECT … FOR UPDATE SKIP LOCKED` as part of the Postgres move |
| — | ℹ️ | AgentMail webhook has no signature check | `api/app.py` | Verify the webhook signature (with I1) |

### J. Reporting
See [Reporting](Reporting) for the report catalog and design.

| # | Item | Notes |
|---|---|---|
| J1 | `aecom/metrics.py` | Single place for KPIs, all with `start`/`end`; money KPIs from the ledger. Today `get_kpis` (orders table) and `get_pnl` (ledger) can disagree |
| J2 | Period parameters | `get_pnl` / `get_kpis` are all-time only |
| J3 | `ops_reports` + `submit_report` | Immutable, versioned, hashed snapshots; code attaches numbers, agents write commentary |
| J4 | Number check | Reject report text that quotes figures not in its data |
| J5 | Daily flash email | Replaces the log-only daily report; merged with the P3 digest; stop writing daily insights to shared memory |
| J6 | Weekly business review | Stored and emailed; product, marketing, support and AI sections |
| J7 | VAT detail on ledger lines | Country and rate per VAT line, needed for the OSS report |
| J8 | Period close | `ops_periods`; no postings into closed months |
| J9 | Month-end close report | P&L, cash, VAT, budget vs actual, payout reconciliation (F4) |
| J10 | Accountant export | General journal CSV (DATEV format for a German accountant), receipts linked to expenses |
| J11 | Immutable records | Append-only ledger, 10-year retention (GoBD); demo backdating only in simulator mode |
| J12 | Governance report | Approvals, denies, kill switch, incidents, GDPR requests, consent, key rotations |
| J13 | Reports page + exports | Dashboard archive, CSV/PDF downloads, `aecom report`, `GET /reports` |
| J14 | Decision review | Quarterly: did each ADR deliver? |

### K. Incident response
See [Incident Response](Incident-Response) for who detects, diagnoses and fixes each problem type.

| # | Item | Notes |
|---|---|---|
| K1 | `ops_errors` with tracebacks | Today agent crashes keep only `"Type: message"`, tool errors keep only `ok=False`, and bus tracebacks live only in rotating process logs |
| K2 | Incident lifecycle | `ops_incidents` with owner, timeline, root cause, fix, linked approvals and PRs |
| K3 | Runbooks as skills | One per monitor; shared by the Sentinel and humans |
| K4 | Sentinel diagnostic tools | Read-only: incidents, errors, run timeline, health, backlog, dead letter, deploys |
| K5 | Sentinel remediation tools | Gated: replay dead letter, resend delivery email, re-run reconciler, pause agents, manage incidents |
| K6 | `aecom doctor` | One-command health and diagnostics report |
| K7 | Safe mode | Unacknowledged P1 after 2 h → agents paused, deliveries and support continue, no spending |
| K8 | Incident → GitHub issue → PR | Engineer agent (e.g. Claude Code in GitHub Actions) opens a PR with a failing test and the fix; human merges; never deploys |
| K9 | Correcting entries | Data fixes prepared by Sentinel as T3 approvals; ledger stays append-only |
| K10 | Postmortems | Every P1; draft by Sentinel, finished by a human; becomes a regression test and lessons |
| K11 | On-call | Backup person with dashboard access; Tailscale SSH from the phone |

### L. Architecture direction: build less, use services (2026-10-25)
Build what makes the company different (the governed agent organisation); use proven services for everything with legal duties or that is already solved.

| # | Item | Notes | Changes these items |
|---|---|---|---|
| L1 | **Lemon Squeezy as system of record** | Checkout, payment, VAT, licence keys, download hosting, refunds and receipts live in Lemon Squeezy. The app is the management layer: agents decide, price, market, support and report. The internal ledger mirrors platform data for the agents | A5 becomes the core of Phase 3. I4–I8, I11, I13 and J7 shrink to "mirror the platform correctly" instead of re-implementing it. Sales keep running when the app host is down |
| L2 | **Off-the-shelf ops tooling** | Sentry (errors with tracebacks), healthchecks.io (external heartbeat), ntfy (phone push), LangSmith (traces). Build only the business monitors: undelivered orders, refund spikes, ROAS, budgets, invariants | K1 becomes Sentry integration plus a thin `ops_errors` link; B heartbeat and push become configuration |
| L3 | **Start with fewer active agents** | Active at launch: Support (event-driven), Finance (daily), Store Ops reflexes (no LLM), CEO (weekly), Researcher (on demand). Others switched on when there is work. Needs a per-agent enabled flag in `ops_settings`, respected by the scheduler and the Foreman | AI cost per order measured from day one (target ≤ €0.50) |
| L4 | **First 10 sales before autonomy** | Launch one product with agents assisting and a human approving almost everything | G4–G6 start only after real approvals exist; becomes the entry gate of Phase 5 |
| L5 | **Phase 0 unchanged; merge the PoC** | Security fixes stay first. Merge `feat/agentic-ecommerce-poc` into `main`; afterwards small PRs with CI | — |

### M. Web access
Today only the Researcher has web access: Anthropic's server-side `web_search` and `fetch_url` (plain HTTP GET, no JavaScript, no login, first 6,000 characters). There is no browser. Agents never create accounts or log in, and that stays a human task (legal responsibility, KYC, platform rules against automated sign-ups, and the risk of a logged-in agent reading untrusted pages).

| # | Item | Notes |
|---|---|---|
| M1 ✅ 2026-10-26 | Server-side web fetch | Replace `fetch_url` with Anthropic's server-side web fetch tool (confirm the current tool version in Anthropic's docs). It runs on Anthropic's side, so it can't reach the host's network. Keep H5 for any remaining local fetching |
| M2 | Optional read-only browser | Playwright MCP through the registry, for JavaScript-heavy public pages. Researcher only; isolated container without access to the home or host network; fresh profile with no credentials or cookies; policy allows navigate, read and screenshot, and **denies** click-to-buy, form submit, login and download; output fenced; rate limited |
| M3 | Scraping policy | Respect `robots.txt` and site terms, low request rates, prefer official APIs, never collect personal data (GDPR). Written into the Researcher's `POLICY.md` and a skill |
| M4 ✅ 2026-10-26 | Explicit "no accounts, no logins" rule | Stated on the Policy and Approvals page; T4 deny rules in `policy.yaml` for sign-up, login and form-submit tools from any MCP server; a chaos test where a web page tries to make an agent sign up |

### W0 review follow-ups (2026-10-26)
Non-blocking findings from the Haiku reviews of Wave 0, to fold into later waves.

| # | From | Follow-up |
|---|---|---|
| R1 | H5 | Pin the checked IP for the actual connection (DNS-rebinding window between check and fetch); also refuse IPv4-compatible (`::a.b.c.d`) and SIIT addresses; a redirect without `Location` should stop immediately |
| R2 | I1 | Parametrized test over every protected route; test that the AgentMail webhook stays reachable; document that changing `AECOM_API_TOKEN` needs a restart |
| R3 | M1 | `max_content_tokens` and optional domain allow/block lists for `web_fetch`; `web_search` default type → `web_search_20260209` and configurable `max_uses`; mention `web_fetch` on the Agents wiki page |
| R4 | M4 | For MCP tools, match forbidden patterns on the tool part only, so a server *named* e.g. `login_service` doesn't deny its read tools; Policy page: `request_provisioning` escalates only for the CEO (others are denied) |
| R5 ✅ | — | Replace FastAPI's deprecated `@app.on_event("startup")` with a lifespan handler |

#### Decisions owned by the product owner
| # | Decision | Status | Items waiting on it |
|---|---|---|---|
| U1 | **Accounting tool** (e.g. lexoffice or sevDesk) instead of building bookkeeping | Owner is evaluating | J8–J11 likely replaced by an API integration; F4 payouts go to the tool; J9 month-end report reads from it |
| U2 | **Hosting** (cloud server vs. Raspberry Pi) | Owner is evaluating | C1–C3, H6, C6 remote access, webhook exposure (Cloudflare Tunnel or public endpoint), backup design |

## Next
1. **Live validation:** run every scenario against real Claude and record the outcomes and costs. Calibrate `PRICE_*`.
2. **Real storefront:** implement the adapter for the CEO's chosen platform, plus webhooks that publish `order.paid`.
3. **Real channels:** ad-platform and social MCP servers granted to Marketing; spend reconciliation into the ledger.
4. **Postgres by default:** Alembic migrations, pgvector embeddings for `memory_items`, a cross-database fix for the dashboard SQL.
5. **Event bus:** Redis Streams or NATS; idempotent consumers; dead-letter queue.
6. **Lessons loop:** apply Judge- and human-approved lessons to `AGENTS.md` automatically, as a versioned PR.
7. **Harness self-management:** versioned prompts, graphs and model tiers in the registry; A/B runs scored by the Judge; human-approved promotion.
8. **Security:** vault-backed `SecretsProvider`, per-tool rate limits, red-team tests for prompt injection, PII redaction in logs.
