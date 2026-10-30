---
name: skill-etsy-digital-listing
kind: skill
status: proposed
candidate_agents: [marketing, store_ops]
version: "1"
last_reviewed: 2026-10-30
---
# Etsy digital-listing rules checklist

F6 (Roadmap). A procedure for listing a digital download on Etsy (F3: Etsy as a second channel). This is a
checklist, not legal advice — Etsy's own Seller Policy and Seller Handbook are the source of truth and change
over time; re-verify before relying on any number here.

## Before you list
- Confirm the file is yours to sell: Etsy's handmade/digital policy requires the seller to have made or hold the
  rights to the digital item. Don't resell someone else's template, font, or art pack without a licence that
  explicitly allows resale.
- Confirm the item isn't on Etsy's prohibited/restricted list (IP-infringing content, hate content, content that
  promotes illegal activity, adult content outside Etsy's mature-content rules, items claiming medical/health
  benefits, etc.) — verify in the current Etsy Seller Policy before publishing anything borderline.

## File types and limits
- Etsy accepts common formats for digital downloads (e.g. PDF, JPG/PNG, MP3/MP4, ZIP, SVG, AI, PSD, DOCX) but the
  **per-file size limit and the maximum number of files per listing are set by Etsy and change** — verify in the
  current Etsy Seller Policy / Seller Handbook before assuming a number; do not hardcode a size limit into store
  copy or automation.
- Etsy must deliver the file: digital items are required to be delivered through Etsy's own digital-download
  mechanism (the buyer downloads from their Etsy purchase page) rather than emailed manually or linked to external
  hosting. This app's own `deliver_order` tool exists because *our* storefront is the platform of record for our
  primary channel — on Etsy, Etsy's delivery mechanism plays that role; don't bypass it with a manual link.

## Listing fields
- **Title**: front-load the most important search term, keep it readable (not keyword-stuffed); avoid ALL CAPS.
- **Tags**: Etsy limits tags to a fixed count and a fixed character length per tag — verify the current count/length
  in the Seller Handbook; use every tag slot, each one a distinct buyer phrase (no repeats of words already in the
  title if a different phrase would cover more ground).
- **Description**: lead with what the buyer gets (file formats, sizes/dimensions, what's included), then usage
  rights (personal/commercial), then delivery note ("instant digital download, no physical item will be shipped").
- **Category / attributes**: set "Type" to digital/download so Etsy's search and checkout treat it correctly.
- **Photos**: show the actual deliverable (mockups of the file in use, a screenshot of its contents/pages), not a
  stock photo unrelated to what's downloaded — this is an Etsy trust-and-safety expectation, not just an SEO one.

## Pricing and fees
- Do not hardcode Etsy's listing fee, transaction fee %, payment processing fee, or currency-conversion fee in any
  prompt, skill, or automation — these vary by seller location and change periodically. Verify the current figures
  in the current Etsy Seller Policy (Fees) before using them in a margin calculation.
- Digital items still have VAT/sales-tax implications depending on the buyer's country; see the EU VAT/OSS
  checklist skill for digital goods sold to EU consumers — Etsy as a marketplace may itself be the party required
  to collect and remit some of these taxes (marketplace facilitator rules); verify against Etsy's current policy
  for which taxes Etsy already handles versus which stay the seller's responsibility.

## Policy cautions
- Don't copy another shop's listing text, photos, or tags.
- Don't offer to complete a transaction off-Etsy to avoid fees — this violates Etsy policy.
- Keep review/feedback solicitation within Etsy's rules (no incentivized reviews).
- Respond to the current Etsy Seller Policy for anything not covered above; policy wording changes, this checklist
  does not auto-update.

## Sources to verify against
- Etsy Seller Policy (etsy.com/legal)
- Etsy Seller Handbook (etsy.com)
- Etsy Fees page (etsy.com/legal)

## Last reviewed
2026-10-30 — unverified against live sources (fetch of etsy.com/legal returned 403/blocked in this session); treat
every figure above marked "verify" as unconfirmed until checked against the live Etsy Seller Policy.
