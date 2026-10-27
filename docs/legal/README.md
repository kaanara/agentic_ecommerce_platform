# Legal templates

> **TEMPLATES ONLY - NOT LEGAL ADVICE.** Nothing in this folder has been reviewed by a lawyer. Do not publish
> any of these pages until a qualified lawyer (German/EU consumer, e-commerce and data-protection law) has
> reviewed and approved them. The agents of this system must not edit, publish or "fill in" these documents.

| File | What it is |
|---|---|
| [impressum.md](impressum.md) | German legal notice (Impressum, § 5 DDG / § 18 MStV) skeleton |
| [privacy-policy.md](privacy-policy.md) | Privacy policy (Datenschutzerklärung, Art. 13/14 GDPR) skeleton listing the processors this system really uses |
| [terms.md](terms.md) | Terms and conditions (AGB) skeleton incl. the digital-content withdrawal-waiver clause |

The internal procedures for data-subject requests and retention are in the wiki:
[GDPR and Retention](../wiki/GDPR-and-Retention.md).

## What the human owner must fill in

Every placeholder is written in `[BRACKETS]`. Find them all with:

```bash
grep -rn "\[[A-Z][A-Z0-9 _/,.()-]*" docs/legal/
```

Before go-live, the owner (not an agent) must:

1. **Identity** - legal name or company name, legal form, authorised representative(s), postal address
   (a P.O. box is not enough for the Impressum), email address and a second fast contact channel.
2. **Registers and tax** - commercial register court and number (if registered), VAT ID (USt-IdNr.)
   or business ID (W-IdNr.), small-business status (§ 19 UStG) if applicable.
3. **Responsible for content** (§ 18 (2) MStV) - name and address, if editorial content is published.
4. **Processors** - confirm the list in the privacy policy matches what is actually enabled in production
   (Sentry, LangSmith, ntfy and healthchecks are optional), sign each provider's DPA (AVV), and record
   where data is transferred outside the EU/EEA and on which basis (adequacy decision, SCCs, DPF).
5. **Legal bases and retention periods** - every `[LEGAL BASIS]` / `[RETENTION]` placeholder; keep them
   consistent with the retention table in the wiki (and check tax retention with the accountant).
6. **Withdrawal waiver** - confirm the checkout consent wording (version `2026-10-v1`) with the lawyer;
   if the wording changes, bump the version (e.g. `2026-11-v2`) in the checkout, in code and in
   [terms.md](terms.md) together.
7. **Data protection officer** - decide whether one is required (§ 38 BDSG); fill in or delete the section.
8. **Supervisory authority** - the competent data-protection authority for the owner's federal state.
9. **Merchant of record** - with Lemon Squeezy as merchant of record, the contract of sale is with
   Lemon Squeezy; let the lawyer decide how these terms and the Lemon Squeezy buyer terms fit together.

Record the lawyer review (who, when, which version) as a decision (`company_decisions`) or in the wiki
before publishing.
