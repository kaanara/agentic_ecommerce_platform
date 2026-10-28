# Development Plan: PoC → production

> **Goal:** run the company 24/7 on a Raspberry Pi 4, selling digital products (licence keys, downloads, courses) to EU customers. Start with a human in the loop, then let agents earn autonomy action by action.
>
> We can't foresee every failure. This plan covers every **known class** of failure and adds chaos testing to find the unknown ones. Item IDs (A1, B3, …) refer to the [Roadmap](Roadmap) → *Production readiness backlog*.

> **Direction (2026-10-25):** build less and use services. See Roadmap → section L. Lemon Squeezy is the system of record for everything a customer touches (L1); standard tools cover errors, heartbeat and push (L2); few agents are active at launch (L3); the first 10 sales come before autonomy (L4). **Decided 2026-10-28:** hosting (U2) = Raspberry Pi first, cloud server once revenue reaches about USD 20–30; accounting (U1) = the built-in ledger until then, then lexoffice or sevDesk with the cloud move. So the *(U2)* items are now built for the Pi, and the *(U1)* items stay deferred until the migration trigger fires.

## Principles
1. **Sales never depend on an LLM.** Order delivery, retries and monitors are plain code. Agents handle judgment.
2. **Test first.** Every reliability fix starts with a chaos test that fails, and ends when it passes.
3. **Nothing goes unnoticed.** Every failure is either recovered automatically or raises an alert.
4. **Least privilege everywhere.** Scoped keys, read-only tools first, human-owned accounts.
5. **Autonomy is earned with evidence and lost automatically.**
6. **Ship small.** Every phase leaves the system working and deployable.

## Phases at a glance
| Phase | Theme | Depends on | Size* | Exit gate |
|---|---|---|---|---|
| 0 | Foundations | — | S | CI green, keys rotated, staging works |
| 1 | Don't lose sales | 0 | M | Order-path chaos tests pass |
| 2 | Pi production baseline | 1 | M | Survives a reboot, alerts reach your phone |
| 3 | Real store, legally ready | 2 | L | **Go-live checklist** passes |
| 4 | Visibility and the decision chain | 2 | M | Any decision traceable end to end |
| 5 | Gradual autonomy | 3, 4 | M | First action promoted on evidence |
| 6 | Growth | 5 | L | Second channel live |

\* Rough sizes: **S** ≤ 2 days, **M** ≈ 1 week, **L** ≈ 2–3 weeks.

---

## Phase 0: Foundations
**Goal:** make change safe before changing anything important.

| ID | Task |
|---|---|
| H1 | GitHub Actions CI: `uv run pytest` + `aecom scenario all --offline` on every push and PR |
| H2 | Staging = your laptop in `LLM_MODE=scripted`; the Pi only runs `main` |
| H3 | Revoke the exposed AgentMail org PAT; create the dedicated `aecom-poc` key |
| H4 | Set a monthly spend limit on the Anthropic Console (a hard cap outside the app) |
| H5 | Block private and loopback addresses in `fetch_url` (SSRF). Today it fetches any http(s) URL, including the Pi's own API and your LAN |
| F1 | `chmod 600 .env`; password manager + 2FA for every human-owned account |
| I1 | Token authentication on every API route |
| I2 | `send_email` recipients restricted to the customer of the current thread or order |
| I3 | Approvals re-check the kill switch before executing |
| L5 | Merge the PoC branch into `main`; small PRs with CI from here on |
| M1 | Replace `fetch_url` with Anthropic's server-side web fetch (no access to the host's network) |
| M4 | "No accounts, no logins" rule: Policy page, T4 deny rules for sign-up / login / form-submit tools |

**Exit:** CI green on `main`; no shared or long-lived org keys in use; `fetch_url("http://127.0.0.1:8000/health")` is refused; unauthenticated API calls return 401; Support can't email an address outside its current case.

## Phase 1: Don't lose sales ✅ exit gate passed 2026-10-26
**Goal:** a paid order is always delivered, or a human is told.

| ID | Task |
|---|---|
| G1 | Invariants: every paid order delivered; ledger balances; no key on two orders; spend ≤ budget; no side effects with the kill switch on (except delivery); no run stuck `running` |
| G2 (part) | Injectors: delivery crash, duplicate `order.paid`, DB locked, Claude API down |
| G3 (part) | `aecom chaos <name>\|all --offline`, seeded, runs in CI |
| A1 | At-least-once event bus: retries with backoff, dead-letter after N attempts |
| A2 | Order reconciler every 15 min |
| A3 | Agent crash → run row set to `error` |
| A4 | Split the kill switch into *pause agents* and *stop selling* |
| I4 | Track `email_sent` separately from `delivered`; resend on retry |
| I5 | Never mint licence keys; restock only from a real key source, otherwise alert |
| I6, I7 | Refunds reverse VAT; partial refunds keep the key and allow the remainder |
| I8 | All 27 EU VAT rates; unknown country → error, not 0% |
| I9 | Monthly marketing budget counts real spend of every campaign active this month |
| I10 | Deduplicate pending approvals; expire stale ones |
| I11 | Transactional outbox: order, ledger lines and `order.paid` in one commit |
| I12 | Money as integer cents or `Decimal` |
| J1, J2 | One metrics module with periods; money KPIs from the ledger (invariants and reports both need it) |
| J7 | Country and VAT rate on every VAT ledger line |
| K1 + L2 | Sentry for errors with tracebacks (agent loop, gate, event bus, API), linked from a thin `ops_errors` table |

Extra invariants for G1: every delivered order has a sent email; refunds ≤ order amount; `vat_payable` equals the VAT of sales minus the VAT of refunds; no order without ledger lines; month-to-date marketing spend ≤ budget.

**Exit:** with a 30% injected delivery failure rate **and** a 30% injected email failure rate, 100% of orders are delivered *and emailed* within 2 reconciler cycles; zero duplicate keys; ledger balanced to the cent after every step; a pause-and-relaunch sequence can't exceed the monthly budget.

## Phase 2: Pi production baseline ✅ exit gate passed 2026-10-26; Pi items (C1-C3, H6) built 2026-10-28 after the hosting decision
**Goal:** runs unattended; you hear about problems before customers do.

| ID | Task |
|---|---|
| C1 *(U2)* | systemd units with `Restart=always` |
| C2 *(U2)* | `scripts/update.sh` (backup → pull → sync → init → restart), with a rollback path |
| C3 *(U2)* | Daily off-host backups using SQLite's online backup (`sqlite3 … ".backup"`), which is safe while the app runs in WAL mode; monthly restore drill |
| C4 | Alembic migrations |
| C5 | Re-sync `TOOLS.md` changes into the allowlist (additive only, human-confirmed) |
| C6 | Dashboard login; remote access only via Tailscale or an SSH tunnel |
| B (core) + L2 | Business P1 monitors (built); external heartbeat via healthchecks.io and push via ntfy (configured); email alerts, `ops_incidents`, real `/health` |
| H6 *(U2)* | Log rotation (journald limits), NTP time sync, SD-card health or move data to a USB SSD (Pi only) |
| L3 | Per-agent enabled flag; launch with Support, Finance, Store Ops reflexes, CEO weekly, Researcher on demand; AI cost per order on the daily flash |
| J3, J4 | `ops_reports` snapshots, `submit_report`, number check |
| J5 | Daily flash email, merged with the P3 digest |
| K2 | Incident lifecycle in `ops_incidents` |
| K6 | `aecom doctor` |
| K7 | Safe mode after an unacknowledged P1 |
| K11 | Backup on-call person; Tailscale SSH from the phone |

**Exit:** pull the power plug → back online, nothing lost, and a heartbeat alert arrives on your phone within 10 min; an injected undelivered order raises a P1 within 15 min.

## Phase 3: Real store, legally ready ✅ buildable parts done 2026-10-27 (live test-mode run, legal review and U1 items are human/pending)
**Goal:** take real money safely and legally.

| ID | Task |
|---|---|
| A5 + L1 | `store/lemonsqueezy.py` adapter. Lemon Squeezy is the system of record for checkout, payment, VAT, keys, downloads and refunds; the app mirrors orders, refunds and payouts into its ledger for the agents. Scope I4–I8, I11, I13 down to "mirror correctly" |
| — | Webhook endpoint: signature verification, idempotency keys, mapping to `order.paid` / `refund.*` / `chargeback.*` |
| — | Exposing webhooks: Cloudflare Tunnel to the webhook route only, never the dashboard |
| F4 *(U1)* | Payout and fee reconciliation into the ledger and the accounting tool |
| G2 (part) | Injectors: lost webhook, duplicate webhook, chargeback after delivery, refund on an already refunded order |
| E1 | Provisioning loop: `approval.resolved` subscriber, credential detection, vendor → `active`, Foreman task |
| E4 | `request_account_need` for every agent, routed to the CEO |
| E5 | Provisioning task: statuses, checklist, "action needed" email or push, reminder after 3 days |
| E6 | Credential verification: test call, scope check, success or failure message |
| E7 | Rejection reasons back to the CEO; duplicate requests merged |
| H7 | Legal pages: legal notice (Impressum), privacy policy, terms |
| H8 | EU digital content: explicit consent to immediate delivery + acknowledgement that the 14-day withdrawal right is lost, recorded per order |
| H9 | GDPR data-subject requests (access, deletion) as a documented **human** procedure; `delete_customer_data` stays T4 for agents |
| H10 | Data retention: how long orders, emails, audit logs and LangSmith traces are kept |
| I13 | Real download delivery: hosted assets, signed and expiring URLs, resend |
| I14 | Marketing consent per customer, unsubscribe handling, suppression list |
| J8, J9 *(U1)* | Period close and month-end close report; likely read from the accounting tool |
| J10, J11 *(U1)* | Accountant export, append-only ledger, 10-year retention; likely replaced by the accounting tool's API |
| — | AgentMail webhook signature verification |
| — | Live validation: run every scenario against real Claude, record outcomes and costs, calibrate `PRICE_*` and budgets |

### Go-live checklist
- [ ] Phases 0–2 exit criteria met
- [ ] All order-path and payment chaos tests pass, offline and in a live test-mode run
- [ ] Test purchase, refund and chargeback in Lemon Squeezy test mode, reconciled in the ledger
- [ ] Legal pages published; withdrawal consent recorded on a test order
- [ ] A P1 alert received on your phone and acknowledged on the dashboard
- [ ] Backup restored successfully on a second machine
- [ ] `AGENTMAIL_LIVE=true` tested with your own address first
- [ ] Budgets set: Anthropic Console cap, agent daily budgets, monthly marketing and vendor budgets
- [ ] A test month closed: month-end report and accountant export reviewed by your accountant
- [ ] The Lemon Squeezy account went through the full provisioning flow: request → notification → checklist → key verified → vendor `active`

## Phase 4: Visibility and the decision chain ✅ exit gate passed 2026-10-27
**Goal:** answer "what happened and why?" in minutes.

| ID | Task |
|---|---|
| D1 | Live panel: agents running now, auto-refresh |
| D2 | Run detail page: one `run_id` → agents, tool calls, approvals, decision |
| D3 | Shortened tool-call arguments and results in `ops_tool_calls` |
| D4 | Web-search cost in `cost_eur`; per-run search cap |
| B (rest) | P2/P3 monitors, daily digest, Health page |
| — | Sentinel agent (read-only): incident reports, proposed fixes |
| K3, K4 | Runbooks as skills; Sentinel diagnostic tools |
| K5, K9 | Gated Sentinel remediation tools; correcting entries for data fixes |
| K8 | Incident → GitHub issue → PR by the Engineer agent (moved up from Phase 6's E3) |
| K10 | Postmortems → regression tests and lessons |
| M3 | Scraping policy in the Researcher's `POLICY.md` and a skill |
| E2 | Vendor MCP onboarding: read-only tools first |
| H11 | LangSmith on, with PII considered in the retention policy (H10) |
| I15 | Memory provenance; shared memories need Judge or human review |
| I16 | Mask licence keys and download links in the audit log and dashboard |
| J6 | Weekly business review, stored and emailed |
| J12 | Monthly governance and compliance report |
| J13 | Reports page, CSV/PDF exports, `aecom report`, `GET /reports` |

**Exit:** for any approval or ADR, the full chain of agents, tools and inputs is visible from one page; an injected bug produces an incident, a Sentinel diagnosis with the traceback, and a GitHub issue that results in a PR with a failing test.

## Phase 5: Gradual autonomy ✅ built 2026-10-28 (promotion stays locked until 10 real sales; live chaos runs against real Claude are still a human-started step)
**Goal:** fewer approvals, without less safety.

**Entry gate (L4):** at least 10 real sales and a meaningful number of real approvals, so the trust report is based on evidence.

| ID | Task |
|---|---|
| G4 | Trust report: per action, escalations, approve-unchanged rate, promote button |
| G5 | Autonomy stages per action in `policy.yaml`; stage stored in `ops_settings`, set by humans only; allow + notify |
| G6 | Automatic demotion on KPI drops, Judge scores, incidents |
| G2/G3 (rest) | Live chaos runs: new prompt-injection variants, price war, ad-platform outage, a seeded "random bad week" |
| — | Refund-abuse and policy-deny-spike detection feeding demotion |

**Exit:** at least one action (e.g. small refunds) promoted on evidence; an injected KPI drop demotes it automatically.

## Phase 6: Growth
| ID | Task |
|---|---|
| F3 | Multi-channel: Etsy for templates, shared key pool, listing sync |
| — | Real ad channels via vendor MCP servers, spend reconciliation |
| F2 | Vault-backed `SecretsProvider` + writable OAuth token store |
| F5, F6 | Policy rules for MCP tools; platform skills |
| E3 | Engineer agent for new integrations (adapters, webhooks) as PRs, building on K8; human review, never deploys |
| — | Postgres + pgvector; durable event bus (Redis Streams or NATS); lessons loop; harness self-management |
| J14 | Quarterly decision review; cohort, LTV and cash-flow forecast reports |
| M2 | Optional isolated, read-only browser for the Researcher (Playwright MCP) |

---

## Risk register
| Risk | Impact | Mitigated by |
|---|---|---|
| Paid order not delivered | Lost customer, chargebacks | A1, A2, B (P1), A5 (MoR delivers keys too) |
| Pi or network outage | Downtime | A5, C1, external heartbeat |
| SD card failure | Data loss | C3 backups, restore drill, USB SSD (H6) |
| Overspend (ads, LLM, vendors) | Money | Policy limits, monthly budgets, H4 Console cap, B alerts |
| Prompt injection via email or web | Unauthorised actions | `fence()`, policy gate, H5, chaos injection variants |
| Dashboard exposed | Anyone approves spending | C6, Tailscale, no port forwarding |
| Leaked API key | Account abuse | F1 scoped keys, H3 rotation, F2 vault |
| VAT / consumer-law mistakes | Fines | MoR, H7, H8 |
| GDPR breach or unhandled request | Fines | H9, H10, PII-aware logging |
| Silent agent degradation | Bad decisions | Judge scores, G6 demotion, Sentinel |
| A schema change breaks the live DB | Downtime | C4, C2 rollback, backups |
| Unauthenticated API reachable | Anyone approves, injects events, flips the kill switch | I1, tunnel only signed webhook routes |
| Customer data exfiltrated via Support email | GDPR breach | I2, I16, injection chaos tests |
| Customers receive invalid licence keys | Refunds, chargebacks, reputation | I5, real key source, P1 alert on restock failure |
| Wrong VAT (missing rates, refunds) | Tax liability | I6, I8, ledger invariants |
| Marketing budget bypass | Overspend | I9, budget invariant |
| Poisoned shared memory | Agents act on planted "facts" | I15 |
| Report contains made-up numbers | Wrong decisions | J1, J4: code computes figures, the LLM only comments |
| Bookkeeping not compliant (GoBD) | Tax audit problems | J8, J10, J11 |
| Root cause can't be found | Same incident repeats | K1 tracebacks, K4, K10 regression tests |
| Owner unavailable during an incident | Prolonged outage or losses | K7 safe mode, K11 backup person |
| Agent signs up or logs in somewhere | Legal exposure, banned or compromised accounts | M4 deny rules, no credentials in any browser (M2), chaos test |
| Needed account never gets created | Blocked work, silent stall | E4–E7 notification, reminders, verification |

## Testing strategy
| Layer | What | When |
|---|---|---|
| Unit and policy | Tools, gate, ledger, VAT | Every commit (CI) |
| Scenarios | 8 business stories, scripted | Every commit (CI) |
| Chaos, offline | Invariants + injectors, seeded | Every commit (CI) |
| Chaos, live | Real Claude judgment under faults | Before each release, weekly in Phase 5 |
| Platform test mode | Real Lemon Squeezy test purchases | Before go-live and after adapter changes |
| Restore drill | Backup → second machine | Monthly |
