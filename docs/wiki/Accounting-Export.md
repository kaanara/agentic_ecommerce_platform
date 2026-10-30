# Accounting export

J10 in the [Roadmap](Roadmap): a general journal for one month, handed to a German accountant, with receipts linked
to expenses. Implemented in `aecom/accountant_export.py` (`export_month`) and the dashboard tab
`dashboard/_acct_export.py` (Govern > Accounting > Accountant export), which calls it.

> **This export is a draft, not a certified accounting document.** It is not tax advice. Every account number is a
> SKR03 **default** the accountant must confirm before the first real import, and the DATEV column layout is
> reconstructed from public format documentation reviewed during training - not checked column-by-column against
> the accountant's current DATEV release in this session. Validate it (a trial import, or a review against the
> accountant's own column mask) before relying on it.

## `export_month(period, fmt, *, by, account_map=None)`

- `period`: `"YYYY-MM"`.
- `fmt`: `"datev"` (a DATEV Buchungsstapel file) or `"csv"` (a plain general journal).
- `by`: who requested the export - required, goes into the audit row (`accountant_export`, `human:<by>`); the detail
  never contains customer emails (ledger `ref`s are order/campaign/vendor ids, not emails) or `.env` values.
- `account_map`: optional overrides for the SKR03 chart mapping (see below).

Returns an `ExportResult(filename, content: bytes, rows, warnings, encoding)`.

### Period rules
- A month that has **not finished yet** (its end is still in the future) is **refused** (`ValueError`) - there is
  nothing to close the books on yet.
- A month that has finished but is **not closed** in `ops_periods` (`db.Period`, see Govern > Accounting > Months)
  only adds a **warning**: `"<period> is not closed in ops_periods: treat this export as provisional..."`. The
  export still runs - an accountant export is often prepared to *decide* whether to close the month.

## The two formats

### `csv` - general journal
One row per raw ledger line (no splitting), columns: `date, txn, account, debit, credit, memo, ref, country,
vat_rate, receipt`. Dot-decimal numbers, UTF-8 - meant to open in any spreadsheet, not accountant-format-specific.

### `datev` - Buchungsstapel
An EXTF file (data category 21, format name `Buchungsstapel`, format version 12): semicolon-separated,
Windows-1252 (`cp1252`) encodable text, decimal comma (`164,00`), `Belegdatum` as `TTMM` (day+month, e.g. `1510`
for 15 October). Three parts:
1. The EXTF meta header (Berater/Mandant numbers, Wirtschaftsjahr-Beginn, the period, `EUR`) - one row.
2. The DATEV column header row - `aecom.accountant_export.DATEV_COLUMNS`, **116 columns** (format version 12).
3. One row per booking.

**Honesty notice on the 116-column list**: the column names are a best-effort reconstruction of the publicly
documented Buchungsstapel v12 format from training knowledge, not copied from a verified, current DATEV PDF during
this session. The columns this module actually fills - `Umsatz (ohne Soll/Haben-Kz)`, `Soll/Haben-Kennzeichen`,
`WKZ Umsatz`, `Konto`, `Gegenkonto (ohne BU-Schluessel)`, `BU-Schluessel` (always left empty, see below),
`Belegdatum`, `Belegfeld 1`, `Buchungstext` - are the ones consistently documented as required for a Buchungsstapel
import, so check those first; the exact names of the remaining (empty) columns may be off by one or two versus the
accountant's current DATEV release. The file always carries `DATEV_DISCLAIMER` as its first warning.

### Berater, Mandant, Wirtschaftsjahr
Read as optional settings with `getattr(get_settings(), "datev_berater_nr", "")`, `"datev_mandant_nr"` and
`"datev_wj_beginn"` (default `f"{year}0101"`, i.e. a calendar fiscal year) - these are not fields on `Settings`
today, so they default to empty/the calendar year unless the lead adds them (see "Follow-ups" below).

## Splitting a double-entry transaction into DATEV bookings
DATEV books single-sided: one row = one `Konto` + one `Gegenkonto` + one amount + a Soll/Haben-Kennzeichen for which
side `Konto` is on. Our ledger posts multi-line, balanced transactions (e.g. a sale: `cash` debit, `revenue` and
`vat_payable` credit, plus `cogs`/`cash` for the cost). Lines are grouped by `txn_id` and split with a **deterministic
waterfall rule** (`_split_txn`):

> List the transaction's debit lines in ledger-insertion order and its credit lines in ledger-insertion order (they
> sum to the same total - the ledger enforces that at post time). Walk both lists together; at each step, take the
> smaller of the two amounts still unconsumed, emit one `(debit_line, credit_line, amount)` pair for it, and
> subtract that amount from both sides. Repeat until both lists are exhausted.

A €119 sale (19% VAT, €40 cost) yields three booking rows: `(cash, revenue, €100)`, `(cash, vat_payable, €19)`,
`(cogs, cash, €40)` - which is how an accountant would naturally decompose it (net sale, VAT, cost of goods). A
refund splits the same way between `refunds` and `vat_payable` against `cash`. This never drops or duplicates
money: the two queues start with equal totals (the ledger's own balance invariant) and every step consumes equally
from both, so the batch is always internally balanced by construction.

Whichever side of a pair is `cash` becomes DATEV's `Konto` (the bank account) and the other side becomes
`Gegenkonto`; the Soll/Haben-Kennzeichen is `S` when `cash` is the debited side (money received) and `H` when it is
the credited side (money paid out). If neither side is `cash` (not expected in today's chart of accounts), `Konto`
is the debit side and the flag is `S`.

**`BU-Schluessel` is always left empty.** VAT is booked as its own separate, manual VAT account line (e.g. `1776`
for 19%) rather than relying on DATEV's automatic tax calculation on a gross revenue account - the ledger already
stores net revenue and VAT as separate lines, so using an automatic-tax BU key here would double the tax. This is a
deliberate default and the single biggest import-risk decision in this file; **confirm it with the accountant**.

## Account mapping (SKR03, all DEFAULTS)
`DEFAULT_ACCOUNT_MAP` in `accountant_export.py`:

| Key | Default | Meaning |
|---|---|---|
| `cash` | 1200 | Bank |
| `revenue_19` / `revenue_7` | 8400 / 8300 | Domestic sales, 19% / 7% USt |
| `revenue_oss` | 8120 | EU cross-border / OSS sales (generic placeholder) |
| `vat_19` / `vat_7` | 1776 / 1771 | USt payable, 19% / 7% |
| `vat_oss` | 1767 | OSS USt payable (generic placeholder) |
| `cogs` | 3400 | Wareneinsatz |
| `marketing_expense` | 4610 | Werbekosten |
| `ai_expense` | 4964 | EDV/software costs (closest standard SKR03 slot) |
| `vendor_expense` | 4900 | Sonstige betriebliche Aufwendungen |
| `refunds` | 8700 | Erlösschmälerungen |

The revenue/VAT account is picked per transaction from the VAT context (country + rate) found on its
`vat_payable` line: 19% and 7% domestic rates map to `revenue_19`/`vat_19` or `revenue_7`/`vat_7`; anything else
(cross-border EU, OSS, or no rate) falls back to `revenue_oss`/`vat_oss`, unless a **country-specific** override
exists - key it as `revenue_oss:<ISO country>` / `vat_oss:<ISO country>` (e.g. `"revenue_oss:FR": "8125"`).

**Every number above is a default.** Override it:
1. Per call: `export_month(period, "datev", by=by, account_map={"cogs": "3980"})`.
2. Company-wide: the ops setting `accounting_account_map` (a JSON object), read with
   `company_settings.get_setting` - no schema change, e.g. `set_setting("accounting_account_map",
   '{"cash": "1800", "revenue_oss:FR": "8125"}')`.

Numbers set either way are treated as already confirmed and are **not** repeated in the
`"default SKR03 account numbers used for: ..."` warning - only accounts still at their untouched SKR03 default are
listed there.

## Receipts
Every ledger line's `ref` is a campaign, vendor, order or agent-run id, never a document in its own right. For each
line, `_receipt_for` resolves the **best available text**:

| `ref` prefix | Resolves to |
|---|---|
| `cmp_...` | `"campaign <name> (<channel>)"` (`company_campaigns`) |
| `ven_...` | `"vendor <name>"` (`company_vendors`) |
| `run_...` | `"agent run <id> (<agent_id>)"` (`ops_agent_runs`) |
| `ord_...` | `"order <id>"` |
| anything else, or no prefix match | the raw `ref` itself |

If an **expense line** (`cogs`, `marketing_expense`, `ai_expense`, `vendor_expense`) has **no `ref` at all**, the
export adds a warning (`"missing receipt: txn ... (<memo>)"`) instead of failing - common today for
`record_expense` calls that don't pass a ref (e.g. the `record_expense` tool). A present-but-unresolved `ref` is
still used as the receipt text (it is at least traceable), so it is not flagged.

## The dashboard tab
Govern > Accounting > **Accountant export**: pick a finished month (the current month is not offered - it can't be
exported yet) and a format, **Prepare export** to see the row count and every warning (missing receipts, default
account numbers, an unclosed period), then a download button for the file. A plain disclaimer at the top repeats
that account numbers are defaults to confirm with the accountant and that this is not tax advice.

## Follow-ups for the lead
- **CLI**: `aecom export datev|csv YYYY-MM --out FILE` (wrap `export_month`, write `result.content` to `--out`,
  print `result.rows` and every warning; `by` from the OS user or a required flag).
- **Settings** (optional, for a real DATEV header): `datev_berater_nr`, `datev_mandant_nr`, `datev_wj_beginn` on
  `Settings` (today read with `getattr(..., default)` so they are safe to omit).
- **Sidebar**: add `Accounting-Export` to `docs/wiki/_Sidebar.md` under Reporting.
- Have the accountant validate (or trial-import) the DATEV column layout once against their real DATEV release and
  record any corrections here.
