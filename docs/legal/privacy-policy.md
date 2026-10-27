# Privacy policy (Datenschutzerklärung)

> **TEMPLATE - NOT LEGAL ADVICE. Must be reviewed by a lawyer before publishing.**
> Skeleton for the information required by Art. 13/14 GDPR. The processor list reflects what this
> system actually uses (see `src/aecom/` and [Integrations](../wiki/Integrations.md)); remove optional
> services that are not enabled in production. Placeholders are in `[BRACKETS]`.

Last updated: [DATE] - Version: [VERSION]

## 1. Controller

[NAME / COMPANY, ADDRESS, EMAIL - same as Impressum]

Data protection officer: [NAME AND CONTACT, OR "not required" - check § 38 BDSG with lawyer]

## 2. What we process, why, and on which legal basis

| Purpose | Data | Legal basis | Retention |
|---|---|---|---|
| Processing your order and delivering the digital product (licence key / download link) | name, email, country, order and product, amount, VAT | [LEGAL BASIS, e.g. Art. 6(1)(b) GDPR] | [RETENTION] |
| Tax and accounting records | order, invoice and payment data | [LEGAL BASIS, e.g. Art. 6(1)(c) GDPR with § 147 AO, § 257 HGB] | [RETENTION - check with accountant] |
| Customer support by email (incl. AI-assisted reading and answering) | your emails and our replies, order reference | [LEGAL BASIS] | [RETENTION] |
| Proof of your consent to immediate delivery and acknowledgement of losing the withdrawal right | timestamp and consent wording version | [LEGAL BASIS] | [RETENTION] |
| Security, fraud prevention, error analysis and audit trail | technical logs, audit entries, error reports | [LEGAL BASIS, e.g. Art. 6(1)(f) GDPR - legitimate interest: secure and correct operation] | [RETENTION] |
| Marketing attribution | acquisition channel / campaign of an order | [LEGAL BASIS] | [RETENTION] |
| [NEWSLETTER / OTHER - only if introduced] | [DATA] | [LEGAL BASIS, e.g. consent Art. 6(1)(a)] | [RETENTION] |

Automated decision-making (Art. 22 GDPR): [STATEMENT - lawyer. Context: AI assistants answer support
emails and may grant refunds within a policy limit; anything above that limit needs human approval.]

## 3. Recipients and processors

| Provider | Role | Data | Location / transfer basis |
|---|---|---|---|
| **Lemon Squeezy** (Lemon Squeezy LLC) | Merchant of record: sells the product to you, takes payment, issues the invoice and handles VAT. For the purchase itself Lemon Squeezy is [INDEPENDENT CONTROLLER / PROCESSOR - lawyer to decide] | name, email, billing address, country, payment data, order | [LOCATION - TRANSFER BASIS, e.g. DPF / SCCs] |
| **AgentMail** | Sending and receiving our emails (delivery emails, support) | email address, email content | [LOCATION - TRANSFER BASIS] |
| **Anthropic** (Anthropic PBC) | AI processing: our AI assistants read customer emails and order context to draft and send answers and to operate the shop | email content, order data contained in it | [LOCATION - TRANSFER BASIS; confirm API data-retention and no-training terms] |
| **Sentry** (Functional Software, Inc.) - *optional* | Error monitoring; sent with personal data minimised (no default PII, no local variables, secrets masked) | technical error data, which may incidentally contain [DATA] | [LOCATION - TRANSFER BASIS] |
| **LangSmith** (LangChain, Inc.) - *optional* | Tracing of AI-assistant runs for debugging and quality | prompts and outputs of AI runs, which may contain email content and order data | [LOCATION - TRANSFER BASIS] |
| **ntfy** - *optional* | Push alerts to the operator's phone | incident summaries (no customer data intended) | [SELF-HOSTED / ntfy.sh - LOCATION] |
| **healthchecks.io** - *optional* | Uptime "dead-man's switch" ping | no personal data (ping only) | [LOCATION] |
| Hosting | [HOSTING PROVIDER / SELF-HOSTED ON OWN HARDWARE] | all of the above (database, backups) | [LOCATION] |
| Tax advisor / authorities | where legally required | accounting records | [LOCATION] |

## 4. International transfers

[DESCRIBE TRANSFERS OUTSIDE THE EU/EEA AND THE SAFEGUARDS (adequacy decision / EU-US Data Privacy
Framework / standard contractual clauses) PER PROVIDER - lawyer]

## 5. Your rights

You have the right to access (Art. 15), rectification (Art. 16), erasure (Art. 17), restriction
(Art. 18), data portability (Art. 20) and to object (Art. 21 GDPR). Where processing is based on consent
you may withdraw it at any time with effect for the future. Contact: [CONTACT EMAIL FOR PRIVACY REQUESTS].

Requests are handled by a human (internal procedure: [GDPR and Retention](../wiki/GDPR-and-Retention.md)).
Records we are legally required to keep (e.g. tax records) are restricted rather than deleted until their
retention period ends. [WORDING - lawyer]

You may lodge a complaint with a supervisory authority, e.g. [COMPETENT DATA PROTECTION AUTHORITY,
NAME AND ADDRESS].

## 6. Is providing data required?

[STATEMENT - e.g. email and country are required to deliver the product and to charge the correct VAT]

## 7. Website, cookies and tracking

[DESCRIBE THE STOREFRONT HOSTING, SERVER LOGS, COOKIES AND ANY ANALYTICS. This system itself configures
no analytics; Lemon Squeezy checkout pages are subject to Lemon Squeezy's own privacy policy.]

## 8. Changes

[VERSIONING NOTE]
