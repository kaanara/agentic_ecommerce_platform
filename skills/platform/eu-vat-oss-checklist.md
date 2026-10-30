---
name: skill-eu-vat-oss-checklist
kind: skill
status: proposed
candidate_agents: [finance, store_ops]
version: "1"
last_reviewed: 2026-10-30
---
# EU VAT / OSS checklist — digital products sold to EU consumers

F6 (Roadmap). A checklist for selling digital products (downloads, licences, courses) to consumers (B2C) in the
EU. **This is a checklist, not tax advice.** The owner must confirm every item below with a qualified accountant
before relying on it; it does not replace the advice already flagged throughout this repo's own VAT code and docs
(see `docs/wiki/GDPR-and-Retention.md`, `docs/wiki/Reporting.md`, `docs/wiki/Accounting-Export.md`).

## Where tax is owed (place of supply)
- For a digital product/service sold B2C inside the EU, VAT is due at the rate of the **customer's country**, not
  the seller's country — this is the place-of-supply rule the EU's 2021 e-commerce VAT changes are built on.
  Verify current scope/exceptions at the official OSS portal before assuming a borderline case qualifies.

## OSS registration (Union scheme)
- A business established in the EU reports this cross-border B2C VAT through **one** Member State (the Member
  State of identification — normally where the business is established), using the **OSS Union scheme**, instead
  of registering for VAT in every customer country separately.
- OSS returns are filed **quarterly**, covering all OSS-reportable sales across every EU customer country in that
  quarter, each broken out by country and rate.
- Verify current registration steps and deadlines at the official EU OSS portal (vat-one-stop-shop.ec.europa.eu) —
  this checklist does not substitute for registering.

## The €10,000 cross-border threshold
- A small EU-established seller whose total cross-border B2C digital/distance sales across *all* other EU
  countries stay under **€10,000 per calendar year** may still charge VAT at their **home** country's rate instead
  of the customer's country's rate (no OSS filing needed for those sales). Once the threshold is crossed (in the
  current or prior year), the customer's-country rate applies from that point, via OSS or per-country
  registration.
- Treat the €10,000 figure as something to reconfirm at the official EU VAT e-commerce portal before relying on
  it for a pricing or registration decision — this checklist can go stale, the regulation can't.

## Evidence of customer location
- Collecting VAT at the right rate depends on correctly determining where the customer is. The standard is **two
  non-contradictory pieces of evidence** (e.g. billing address + IP address, or billing address + bank/card issuing
  country) for most digital-service sales. If the two pieces disagree, don't guess — escalate instead of booking
  the sale at a rate you're not sure of.
- Keep the evidence used for each sale on file for as long as the related accounting records are kept (see
  retention below) — it's what justifies the rate charged if challenged.

## Rates per country
- **Don't hardcode a rate here.** The current standard rates per EU country live in `VAT_RATES` in
  `src/aecom/store/adapter.py` (`vat_rate(country)`), which is exactly what `record_sale`/ledger code uses to book
  the `vat_payable` ledger line with `country` and `vat_rate` on it. If a rate looks wrong, fix it there (and get
  it reviewed) — don't improvise a different number in a prompt, email, or report.
- Rates change (EU member states adjust standard/reduced rates periodically); re-verify the table in `adapter.py`
  against the official source before a bulk price update, not just once.

## Invoices and records
- Issue/keep a record for every sale showing at minimum: date, customer country, net amount, VAT rate applied, VAT
  amount, gross amount — this app books all of that onto the `vat_payable` ledger line per order already.
- **Germany**: accounting records (invoices, ledger) are expected to be kept for **10 years** (§ 147 AO, § 257
  HGB) — this repo's own retention policy already treats ledger/order records this way (see
  `docs/wiki/GDPR-and-Retention.md`); don't shorten that for OSS-related records without an accountant's sign-off.

## Marketplace / merchant-of-record (MoR) note
- If sales go through a marketplace or merchant-of-record (Etsy, Lemon Squeezy, Paddle, etc.) acting as deemed
  supplier, **the MoR may charge, collect, and remit the VAT itself** instead of the seller filing OSS for those
  sales. In that case this app's ledger still records a `vat_payable` line per order (country, rate) for
  bookkeeping, but the OSS filing obligation may sit with the MoR, not the seller — verify per-platform which
  party is responsible before assuming a duplicate filing is needed.

## Where this app records VAT
- Each sale books a `vat_payable` ledger line with `country` and `vat_rate` (`src/aecom/ledger.py`,
  `record_sale`/`record_refund`).
- The month-end report groups VAT payable by country and rate for the period (`src/aecom/month_end.py`,
  `vat_by_country_rate` / the `vat` section of the month-end report) — this is the number to reconcile against an
  OSS return.
- The accountant export (`src/aecom/accountant_export.py`, CSV and DATEV formats) includes `country`/`vat_rate` per
  ledger row so the accountant can tie each line back to the OSS filing; the DATEV mapping is a **draft** default
  that the accountant must confirm before real use (see `docs/wiki/Accounting-Export.md`).

## Disclaimer
This is an operational checklist for this app's agents, not tax advice. VAT/OSS rules, thresholds, and rates
change, and edge cases (physical+digital bundles, B2B vs B2C, non-EU sellers, UK post-Brexit VAT, marketplace
deemed-supplier rules) are not fully covered here. **The owner must confirm every figure and decision against a
qualified accountant** before relying on this checklist for pricing, registration, or filing.

## Sources to verify against
- EU VAT e-commerce / One Stop Shop portal (ec.europa.eu, vat-one-stop-shop.ec.europa.eu)
- Bundeszentralamt für Steuern — BZSt (bzst.de)

## Last reviewed
2026-10-30 — verified against live sources: fetched vat-one-stop-shop.ec.europa.eu's OSS overview and OSS
registration pages in this session (confirms the place-of-supply rule, Union-scheme registration-by-one-Member-
State mechanism, and that the Member State of identification is normally where the business is established). The
€10,000 threshold, two-non-contradictory-evidence rule, quarterly filing cadence, and the German 10-year retention
figure were not re-fetched this session (bzst.de returned 403; the specific threshold page returned 404) — they
match this repo's existing `docs/wiki/GDPR-and-Retention.md`/`Reporting.md` figures and well-known OSS rules, but
treat them as unverified against live sources until re-checked directly.
