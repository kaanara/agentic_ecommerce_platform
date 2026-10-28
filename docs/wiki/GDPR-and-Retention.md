# GDPR and Retention

> **Not legal advice.** This page is an operating procedure for the human owner. Legal bases, deadlines and
> retention periods must be confirmed by a lawyer (data protection) and the accountant (tax). Every
> `[DECISION: ...]` is an open decision. The customer-facing texts are templates in
> [`docs/legal/`](../legal/README.md).

## H9 - Data-subject requests (human procedure)

Data-subject requests are handled **only by a human**. Agents may forward a request (e.g. the support agent
receives "please delete my data" by email) but never execute it: `delete_customer_data` is **T4** in
`src/aecom/policy/policy.yaml` (never by an agent, not even with approval). Exporting, correcting or deleting
personal data is done by the owner with the steps below.

### 0. Intake

1. Log the request: date received, requester email, request type (access / export, rectification, erasure,
   restriction, objection). Deadline: **one month** from receipt (Art. 12(3) GDPR; extendable in complex cases
   - [DECISION: confirm with lawyer]).
2. Verify identity: reply to the email address stored in `commerce_customers.email` (the requester must
   control that address). Never send data to a different address than the one on record.
3. Record the request in the audit log (from a Python shell with the production `DATABASE_URL`):

   ```python
   from aecom.audit import audit
   audit("human:<your-name>", "gdpr_request", "info", {"type": "erasure", "customer_id": "cus_...", "received": "2026-10-27"})
   ```

   Log the customer **id**, not the email, so the audit entry itself does not need erasing later.
4. Before any change: stop the scheduler/API and take a copy of the database (SQLite: copy `data/aecom.db`
   and `data/aecom.checkpoints.db`; Postgres: `pg_dump`). [DECISION: backup procedure - not defined yet]

### 1. Where personal data lives

Table names are from `src/aecom/db.py`.

| Location | Personal data | Link to the customer |
|---|---|---|
| `commerce_customers` | email, name, country, acquisition_channel | `id` (= `cus_...`), `email` |
| `commerce_orders` | purchase history, amounts, VAT, withdrawal consent (`withdrawal_consent_at`, `withdrawal_consent_version`), store `external_id` | `customer_id` |
| `commerce_refunds` | refund reasons (free text, may quote the customer) | `order_id` |
| `commerce_license_keys` | which key was sold to whom | `order_id` |
| `ledger_entries` | order references in `ref` / `memo` (no email expected) | `ref` = order id |
| `ops_outbox_emails` | full email bodies, in and out | `to`, `sender`, `body` |
| `ops_audit_log` | **append-only**; `detail` JSON may contain emails or names from tool arguments | text search in `detail` |
| `memory_items` | agent memories may mention customers | text search in `content` |
| `ops_errors` | `message`, `traceback`, `context` may contain emails (secrets are masked, personal data is not) | text search |
| `ops_events` | event `payload` (e.g. `order.paid`, inbound email events) | text search in `payload` |
| `ops_approvals` | `args`, `context`, `result` of escalated tool calls (e.g. refunds, emails) | text search |
| `ops_agent_runs` | `task` / `output` of agent runs (e.g. "answer this email from ...") | text search |
| `ops_webhook_receipts` | provider event ids only (no personal data expected) | - |
| LangGraph checkpoints | agent conversation state; `data/aecom.checkpoints.db` (on Postgres: same database) | text search / thread id |
| LangSmith traces (optional) | prompts and outputs of agent runs | search in the LangSmith UI by email |
| Sentry (optional) | error events (PII minimised: `send_default_pii=False`, no local variables) | search in the Sentry UI |
| AgentMail | the mailbox itself (sent/received messages) | AgentMail console |
| Lemon Squeezy | the merchant-of-record copy of customer and order | Lemon Squeezy dashboard |
| Backups | copies of all of the above | expire per the retention table below |

### 2. Find a customer's data (SQL)

Use the production database read-only first. SQLite example (`sqlite3 data/aecom.db`); on Postgres use
`psql "$DATABASE_URL"` and replace `LIKE` with `ILIKE` and `detail` with `detail::text` for JSON columns.

```sql
-- 1. the customer
SELECT * FROM commerce_customers WHERE lower(email) = lower('jane@example.com');
-- note the id, e.g. cus_abc123; below :cid = that id, :email = the address

-- 2. orders, refunds, keys, ledger lines
SELECT * FROM commerce_orders WHERE customer_id = :cid;
SELECT r.* FROM commerce_refunds r JOIN commerce_orders o ON o.id = r.order_id WHERE o.customer_id = :cid;
SELECT k.id, k.product_id, k.status, k.order_id FROM commerce_license_keys k
  JOIN commerce_orders o ON o.id = k.order_id WHERE o.customer_id = :cid;
SELECT * FROM ledger_entries WHERE ref IN (SELECT id FROM commerce_orders WHERE customer_id = :cid);

-- 3. emails
SELECT id, direction, created_at, sender, "to", subject FROM ops_outbox_emails
  WHERE lower("to") LIKE '%' || lower(:email) || '%' OR lower(sender) LIKE '%' || lower(:email) || '%'
     OR body LIKE '%' || :email || '%';

-- 4. free-text / JSON mentions (search for email, id and name)
SELECT id, ts, actor, action FROM ops_audit_log WHERE detail LIKE '%' || :email || '%' OR detail LIKE '%' || :cid || '%';
SELECT id, agent_id, namespace FROM memory_items WHERE content LIKE '%' || :email || '%';
SELECT id, ts, component FROM ops_errors WHERE message LIKE '%' || :email || '%' OR traceback LIKE '%' || :email || '%' OR context LIKE '%' || :email || '%';
SELECT id, ts, topic FROM ops_events WHERE payload LIKE '%' || :email || '%' OR payload LIKE '%' || :cid || '%';
SELECT id, tool_name, status FROM ops_approvals WHERE args LIKE '%' || :email || '%' OR context LIKE '%' || :email || '%';
SELECT id, agent_id, started_at FROM ops_agent_runs WHERE task LIKE '%' || :email || '%' OR output LIKE '%' || :email || '%';
```

In the `sqlite3` shell, set parameters with `.parameter set :cid "'cus_abc123'"` and
`.parameter set :email "'jane@example.com'"`. Also search for the customer's **name** where it is unusual
enough to be meaningful.

### 3. Access / export (Art. 15, Art. 20)

1. Run the queries in step 2 and export the results, e.g. in `sqlite3`:
   `.mode json` then `.once /tmp/export-cus_abc123-orders.json` before each query.
2. Add the external copies: AgentMail messages, Lemon Squeezy order data (or point the customer to Lemon
   Squeezy as merchant of record - [DECISION: lawyer]), LangSmith/Sentry entries if any.
3. Leave out data about other people and secrets (licence keys of *other* orders, internal notes about third
   parties). The customer's own licence keys may be included.
4. Send the export to the verified email address (encrypted archive or download link -
   [DECISION: delivery method]). Delete the local `/tmp/export-*` files afterwards.
5. Audit: `audit("human:<you>", "gdpr_request_closed", "info", {"type": "access", "customer_id": ...})`.

### 4. Rectification (Art. 16)

Correct the field directly (e.g. `UPDATE commerce_customers SET name = '...' WHERE id = :cid;`).
Do not change `country`, amounts or VAT of **past** orders: those are tax records - correct them with an
accounting entry instead ([DECISION: check with accountant]). Audit the change.

### 5. Erasure (Art. 17)

Erasure is limited by legal retention duties: orders, invoices and ledger entries must be kept for the tax
retention period (see H10). For those, **restrict** processing and remove what is not needed for tax.

1. Check for open obligations: undelivered or paid-but-not-refunded orders, open disputes, open approvals.
   Finish or reject those first.
2. **Delete** (not needed for tax):
   - `ops_outbox_emails` rows to/from the customer that are not part of the tax record
     ([DECISION: are delivery emails / invoices kept as commercial letters? - accountant]);
   - `memory_items` rows mentioning the customer;
   - `ops_errors`, `ops_events` (only `processed = 1` rows), `ops_agent_runs` rows mentioning the customer,
     or redact the matching text instead of deleting the row;
   - LangGraph checkpoints of threads about the customer; LangSmith traces; Sentry events;
   - the AgentMail messages in the AgentMail console.
3. **Pseudonymise** what must be kept: keep `commerce_customers.id` and `country` (VAT evidence) and replace
   the identifying fields, e.g.
   `UPDATE commerce_customers SET email = 'erased+' || id || '@invalid', name = '', acquisition_channel = NULL WHERE id = :cid;`
   ([DECISION: lawyer/accountant - which fields the tax record needs, e.g. name on invoices]).
   Orders and ledger lines reference only the customer id, so they stay consistent.
4. **Audit log (`ops_audit_log`)**: the table is append-only - the application refuses to update or delete
   rows (`_audit_is_append_only` hook in `src/aecom/db.py`), because it is the evidence of what agents and
   humans did. Rules:
   - Do **not** delete audit rows to process an erasure. Their legal basis is [DECISION: e.g. legitimate
     interest / accountability, Art. 6(1)(f), Art. 5(2) GDPR - lawyer].
   - Prefer prevention: audit entries should reference ids (`cus_...`, `ord_...`) rather than emails, so
     pseudonymising `commerce_customers` makes them non-identifying.
   - If audit rows contain the email or name in `detail` and the lawyer decides they must be redacted, this
     is a deliberate, documented exception done with raw SQL outside the application (the ORM hook does not
     apply), on a backup-verified database, e.g.
     `UPDATE ops_audit_log SET detail = replace(detail, :email, '[erased]') WHERE detail LIKE '%' || :email || '%';`
     Then append an audit entry recording the redaction (count of rows, customer id, legal reason).
5. Backups: erased data remains in backups until they expire (see H10). If a backup is ever restored, re-apply
   the erasure log ([DECISION: keep a list of erased customer ids for this purpose]).
6. Lemon Squeezy, as merchant of record, keeps its own records; tell the customer to contact Lemon Squeezy or
   forward the request ([DECISION: lawyer]).
7. Confirm completion to the customer and audit `gdpr_request_closed` with the customer id.

### 6. Restriction and objection (Art. 18, Art. 21)

Mark the customer as restricted ([DECISION: no dedicated column yet - for now keep a list in the request
log and add `restricted` to `ops_settings` or a future column]) and stop marketing use
(`acquisition_channel` attribution, campaigns). Tell the agents via the persona/memory only by id, never by
copying the personal data.

## Marketing consent

Marketing email (newsletters) is sent **only with a recorded opt-in** (`src/aecom/consent.py`, I14). Order,
delivery and support emails are not marketing and are not affected.

**Where consent is stored**

| What | Where |
|---|---|
| Opt-in flag, time and source (`checkout`, `form`, ...) | `commerce_customers.marketing_consent`, `marketing_consent_at`, `marketing_consent_source` |
| Withdrawal | `commerce_customers.unsubscribed_at` (consent set to false) |
| Addresses that must never get marketing email (customer or not) | `commerce_suppressions` (`email` lower-case, `reason` = `unsubscribe` / `bounce` / `complaint` / `manual`, `created_at`) |
| Every change | `ops_audit_log`, actor `system`, actions `marketing_opt_in` / `marketing_unsubscribe` / `marketing_suppressed`, detail holds the **customer id** (never the address) and the source or reason |

Existing customers were migrated with `marketing_consent = false`: nobody receives a newsletter until they opt in.
`send_newsletter` sends only to addresses where `consent.is_marketable()` is true (consent recorded, not
unsubscribed, not suppressed), reports `sent` and `skipped_no_consent`, and ends every newsletter with
"Reply STOP to unsubscribe".

**Export proof of consent** (e.g. for an access request or a complaint to the authority):

```sql
SELECT id, email, marketing_consent, marketing_consent_at, marketing_consent_source, unsubscribed_at
FROM commerce_customers WHERE lower(email) = lower(:email);
SELECT reason, created_at FROM commerce_suppressions WHERE email = lower(:email);
SELECT ts, action, detail FROM ops_audit_log
WHERE action LIKE 'marketing_%' AND json_extract(detail, '$.customer_id') = :customer_id ORDER BY ts;
```

(PostgreSQL: `detail->>'customer_id' = :customer_id`.) Keep consent evidence as long as the customer record,
and the suppression row even after erasure (it holds only the address, to keep honouring the objection) -
[DECISION: confirm with lawyer].

**Unsubscribes, bounces, complaints**

* A reply whose subject or first line is `STOP`, `UNSUBSCRIBE` or `abmelden` (case-insensitive, also after
  `Re:`) is handled before Support: the sender is unsubscribed (`unsubscribed_at`, suppression `unsubscribe`),
  gets a short confirmation in the same thread, and no agent runs. Repeats change nothing.
* AgentMail webhooks `message.bounced` and `message.complained` suppress every listed recipient with reason
  `bounce` / `complaint` (signed and de-duplicated like the other webhooks).
* A new opt-in after an `unsubscribe` lifts that suppression. A `bounce`, `complaint` or `manual` suppression
  is **not** lifted by an opt-in: the owner checks the address and, only if it is valid and the person asked
  again, deletes the row from `commerce_suppressions` and audits `marketing_suppression_removed` with the
  customer id.
* An objection to marketing (Art. 21(2)) is handled with `consent.unsubscribe(email)` (see section 6).

## LangSmith tracing

LangSmith is optional and **off by default**. Enabling it is a conscious decision, taken only after the lawyer review.

**What is sent.** Every traced run: full prompts, model outputs, tool arguments and tool results. These can contain
customer emails, names, order details and support messages. Masking of secrets does not remove personal data.

**How to enable** (environment, exported automatically): `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY=<key>`,
`LANGSMITH_PROJECT=<project>`. To disable, unset `LANGSMITH_TRACING` (or set it to `false`) and restart.

**Open questions to settle first** [DECISION: lawyer / owner]
- Data location: LangSmith offers an EU and a US region. Choose the EU region if available for the account; if US is
  used, a transfer basis (e.g. SCCs / DPF) and a data processing agreement are needed, and the privacy policy must name LangSmith.
- Retention in LangSmith: set the project retention (see the retention table, e.g. 14-30 days); the default may be longer.
- Whether tracing is allowed on production data at all, or only on synthetic/test runs.

**Erasure request.** Search the project for the customer's email and delete the matching traces (LangSmith UI: filter
runs by the email text, select, delete; or the API's delete-runs call). Record this in the erasure checklist (step 5).
Traces also expire with the project retention setting.

## H10 - Retention

Periods are proposals to be decided; "10 years" for accounting records follows German tax and commercial law
(§ 147 AO, § 257 HGB) and must be **checked with the accountant** (some document classes have 6 or 8 years).
Retention periods start at the end of the calendar year of the record.

| Data | Where | Proposed retention | Basis / note | Deletion mechanism |
|---|---|---|---|---|
| Orders, refunds | `commerce_orders`, `commerce_refunds` | **10 years** | tax records (§ 147 AO) - check with accountant | manual yearly purge [DECISION] |
| Ledger | `ledger_entries` | **10 years** | books of account (§ 147 AO, § 257 HGB) - check with accountant | manual yearly purge [DECISION] |
| Withdrawal consent evidence | `commerce_orders.withdrawal_consent_*` | same as order | proof of consent for the withdrawal waiver | with the order |
| Customer master data | `commerce_customers` | as long as an order is retained; [DECISION: pseudonymise after N years without purchase] | contract / tax | pseudonymise (see erasure) |
| Licence keys | `commerce_license_keys` | as long as the licence is valid + [DECISION] | contract performance | manual |
| Emails (support, delivery) | `ops_outbox_emails`, AgentMail | [DECISION: e.g. 2 years after last contact; invoices / commercial letters 6 years - accountant] | support history | [DECISION: scheduled purge job - not implemented yet] |
| Audit log | `ops_audit_log` | [DECISION: e.g. 10 years, aligned with the ledger, since it evidences financial actions] | accountability; append-only | archive and drop old rows outside the app [DECISION] |
| Error logs | `ops_errors`, Sentry | [DECISION: e.g. 90 days] | debugging | [DECISION: purge job; Sentry project retention setting] |
| Events, agent runs, tool calls | `ops_events`, `ops_agent_runs`, `ops_tool_calls` | [DECISION: e.g. 1 year; runs with cost data as long as the related ledger entry?] | operations, cost evidence | [DECISION] |
| Approvals | `ops_approvals` | [DECISION: e.g. same as audit log] | evidence of human decisions | [DECISION] |
| Agent memory | `memory_items`, `memory_lessons` | [DECISION: review yearly; must not hold customer personal data] | operations | manual review |
| LangGraph checkpoints | `data/aecom.checkpoints.db` | [DECISION: e.g. 30 days] | conversation state | [DECISION] |
| LangSmith traces | LangSmith | [DECISION: e.g. 14-30 days; LangSmith project retention setting] | debugging | provider setting |
| Webhook receipts | `ops_webhook_receipts` | [DECISION: e.g. 90 days - only needed for de-duplication] | idempotency | [DECISION] |
| Incidents, reports | `ops_incidents`, `ops_reports` | [DECISION] | operations (no customer data expected) | [DECISION] |
| Backups | [BACKUP LOCATION] | [DECISION: e.g. daily for 7 days, weekly for 4 weeks, monthly for 12 months] | recovery; erased data persists until expiry | rotation [DECISION] |

When a decision is made, record it as an ADR (`company_decisions`, category `compliance`), update this table
and the privacy policy template ([`docs/legal/privacy-policy.md`](../legal/privacy-policy.md)) together.
No automatic purge job exists yet; until one does, the owner runs a yearly manual review.
