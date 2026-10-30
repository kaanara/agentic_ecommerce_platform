---
name: skill-shopify-seo-checklist
kind: skill
status: proposed
candidate_agents: [marketing, store_ops]
version: "1"
last_reviewed: 2026-10-30
---
# Shopify product-page SEO checklist

F6 (Roadmap). A checklist for publishing or editing a product page on Shopify so it's findable in search. Verified
against Shopify Help Center pages fetched in this session (see "Last reviewed"); re-check before relying on an
exact character count, since Shopify's own guidance can change.

## Page title (SEO title, not the product name shown on the page)
- Shopify allows up to 70 characters, but recommends keeping it to **60 characters or fewer** so it isn't truncated
  in search results.
- Put the most important keyword near the start, keep it human-readable (not a keyword list), and make every
  product's title unique.

## Meta description
- Shopify recommends around **160 characters**. Use plain, direct language that sells the click — what the buyer
  gets, why this product — and work in the product's main keyword naturally.
- Every product/page needs its own meta description; don't leave Shopify's auto-generated fallback (first lines of
  body copy) for anything that matters for SEO.

## URL handle
- Keep it short, lowercase, hyphen-separated, and close to the title's main keyword (Shopify generates it from the
  title by default — edit it before publishing, since changing a live URL later needs a redirect).
- Avoid dates, SKUs-only, or redundant words already implied by the collection ("store/products/..." already
  signals "product").

## Structured data (product schema)
- Modern Shopify (Online Store 2.0) themes emit product JSON-LD (price, availability, rating if present)
  automatically in most cases; don't hand-roll duplicate schema unless the theme doesn't already emit it.
- After publishing, check the page with a structured-data testing tool (e.g. Google's Rich Results Test) rather
  than assuming the theme's markup is complete/valid — theme-to-theme coverage varies.

## Images and alt text
- Every product image needs alt text describing what's shown (product, color/variant, context) using natural
  language — not a keyword dump, not left blank.
- Compress images before upload (large unoptimized images are the most common Shopify page-speed problem); use
  Shopify's built-in image CDN/responsive sizing rather than oversized source files.

## Canonical tags
- Shopify sets canonical URLs automatically for most page types (important when the same product appears under
  multiple collections, which otherwise look like duplicate content). Don't fight it with manual `rel=canonical`
  overrides unless you have a specific, verified reason — check the rendered `<head>` if duplicate-content issues
  show up in Search Console.

## Site/collection structure
- Keep collections shallow and logical (a buyer should reach any product in 2–3 clicks from the homepage).
- Use breadcrumbs (theme-provided) so both users and search engines understand the hierarchy.
- Link related/complementary products from each product page (internal links help indexing and discovery).

## Speed
- Minimize third-party apps/scripts injected into the storefront; each one can add render-blocking JS.
- Optimize/compress images (see above) — this is consistently the largest, most controllable speed lever on a
  Shopify storefront.
- Review Shopify's built-in store speed report periodically; don't rely on a one-time audit.

## Sources to verify against
- Shopify Help Center — "Improving search engine optimization (SEO)" (help.shopify.com)
- Shopify Help Center — "Adding keywords for SEO to your Shopify store" (help.shopify.com)
- Shopify Help Center — "Online store images" (help.shopify.com)

## Last reviewed
2026-10-30 — verified against live sources: fetched help.shopify.com's SEO overview, "Adding keywords for SEO"
(confirms the 70/60-character title guidance and the ~160-character meta description guidance above), and the
images section. Structured-data, canonical, and collection-structure guidance above is general Shopify/SEO practice
not re-confirmed against a specific live page this session — verify against help.shopify.com if anything here looks
stale.
