# Team 1 (Kaya Kopi) — Data Audit

Audit of the client data package delivered 16 Nov 2026.
Sources: `sql/kayakopi_pos.sql`, `tabular/*.csv` (8 outlet files),
`images/supplier_invoices/*.jpg` (69 scans), `docs/emails/*`, `docs/whatsapp_exports/*`.

> Rule of thumb applied: anything a number *implies* but the row doesn't state is
> flagged, not silently "fixed".

## 1. Sources inventory

| Source | Records | Coverage | Format |
|---|---|---|---|
| `sql/kayakopi_pos.sql` → `pos_transactions` | 33,059 | 2025-07-01 → 2026-06-30 | ISO dates |
| `sql/kayakopi_pos.sql` → `supplier_purchases` | 178 | 2025-07-05 → 2026-06-28 | **DD/MM/YYYY** |
| `tabular/daily_summary_*.csv` (8 files) | 344–353 each | 2025-07-01 → 2026-06-30 | mixed (see §4) |
| `images/supplier_invoices/` | **69** scans | 2025-07 → 2026-06 | JPEG, scanned |
| `docs/emails/`, `docs/whatsapp_exports/` | 2 + 1 | — | text |

> NOTE: `CLIENT_BRIEF.md` and the PDF both say **70** scanned invoices; the folder
> actually contains **69**. → **RESOLVED (client, 18 Nov):** 69 is the complete set —
> "70" was the count of hardcopies pulled, and two shared one invoice number, so only
> one scan survived. **No 70th file to send.** (Q2 / Trap T2.)

## 2. POS transactions (`pos_transactions`)

- 33,059 rows; 12 outlet codes **but 13 distinct `outlet_code` values** because both
  `AMK-03` (1,290 rows) and `AMK-3A` (1,180 rows) appear (see Trap T1).
- `payment_mode` values: `cash`, `card`, `transfer` — no nulls.
- **Two prices per menu item**: for each item the table holds a base price and one
  exactly `× 1.35` higher. → **RESOLVED (client, Q6):** these are **not a GST effect** —
  `$1.60` is the **kopitiam price**, `$2.16` the **mall price** (mall stalls charge **35% more**
  for rent). Every POS price is **GST-inclusive** (GST = 9% throughout). ⇒ Compute revenue as
  **takings** (sum of prices paid); do any ex-GST margin arithmetic once and **label it** (Trap T3).
- No `NULL`s in required columns; `tx_id` is a clean surrogate key.

## 3. Supplier purchases (`supplier_purchases`)

- 178 rows; 5 suppliers: SUP-01 Heng Huat Coffee Roasters, SUP-02 Sunrise Bakery
  Supplies, SUP-03 Ban Seng Eggs & Provisions, SUP-04 Mewah Sugar Trading,
  SUP-05 **D'Best Frozen Foods** (apostrophe in name — CSV-escaping hazard).
- All rows carry `gst_treatment = 'ex-GST'`, but the matching **paper invoices are
  GST-inclusive** (see §5). → **RESOLVED (client, Q7):** `total_amount` is the **ex-GST
  subtotal** computed by the old system from a **stale price list**, so the table will
  **never tally** with a hardcopy even after adding 9%. **Paper invoices are the source of
  truth for supplier costs** — trust the **lines** (if a hardcopy's lines ≠ its printed
  total, flag that invoice). DB `total_amount` = baseline/matching only, do not reconcile to
  it (Trap T4).
- **Duplicate `invoice_no`**: `INV-042509-752` appears twice with different dates
  (05/09/2025 = S$1,135; 11/09/2025 = S$887). → **RESOLVED (client, Q8):** **two genuine
  deliveries** — supplier Mewah **reused a number** (their error). **Keep both rows**; the
  single hardcopy is matched **by amount + date**, the other is tagged "no scan" (Trap T5).
- Dates stored as `DD/MM/YYYY` — the only ISO-free source.

## 4. Daily outlet CSVs (8 files)

| File | rows | date fmt | extra column |
|---|---|---|---|
| AMK-01 | 344 | DD/MM/YYYY | Remarks |
| AMK-03 | 344 | DD/MM/YYYY | (txns) |
| BDK-01 | 353 | DD/MM/YYYY | WASTAGE_KG |
| JUR-01 | 350 | DD/MM/YYYY | Remarks |
| TAM-01 | 348 | DD/MM/YYYY | (txns) |
| TPY-02 | 344 | **MM/DD/YYYY** | Weather |
| WDL-01 | 344 | DD/MM/YYYY | WASTAGE_KG |
| YIS-02 | 345 | **MM/DD/YYYY** | Weather |

- **Two files (TPY-02, YIS-02) use US `MM/DD/YYYY`**, the other six use SG
  `DD/MM/YYYY`. A naive `datetime.strptime('%d/%m/%Y')` silently corrupts the two US
  files (Trap T6).
- Headers are **not uniform** across files: `Date`/`date`/`DAY`/`Business Date`;
  `Total Sales (SGD)`/`sales$`/`REVENUE`/`Gross Takings`; `No of Cups`/`txns`/`CUPS`/
  `Customer Count`. No single canonical schema (Trap T7).
- **Missing days** within the 2025-07-01→2026-06-30 window: AMK-01 21, AMK-03 21,
  BDK-01 12, JUR-01 15, TAM-01 17, TPY-02 20, WDL-01 21, YIS-02 20. → **RESOLVED (client, Q13):**
  an absent date = **manager did not file that day** — **exclude from averages and show as a
  gap, never zero-fill**. Closures are **only the first two days of Chinese New Year** (not the
  bulk of these). Known unfiled case: **TPY-02 30 Jun 2026** (Kumar did not file). (Trap T8.)

## 5. Scanned invoices (69 JPEGs)

OCR-checked a representative sample. Consistent findings:
- Every paper invoice is **GST-inclusive**: `Subtotal`, then `GST (9%)`, then `TOTAL`.
  The DB row for the same `invoice_no` stores the **ex-GST** amount only (Trap T4).
- **Amount mismatches** between paper `Subtotal` and the DB row, e.g.:
  - INV-052507-964: paper 599.05 vs DB 600.00
  - INV-052606-498: paper 1226.00 vs DB 1080.00
  - INV-042509-752: paper 1155.36 vs DB 1135.00 (and a second DB row 887.00)
  - INV-032602-477: paper 19.30 vs DB 17.80
- **Inconsistent GST Reg Nos** on invoices from the same supplier (e.g. D'Best shows
  `M2-1636016-1` and `M3-9325232-5`). → **RESOLVED (client, Q9):** the client **cannot
  confirm which is current and asks us not to guess** — identify suppliers by **supplier
  code, never by GST reg no**, and hand the inconsistent-number list to **Finance** to query
  the suppliers (Trap T9).
- All 69 scans matched an `invoice_no` in `supplier_purchases`; 108 DB rows have no scan.

## 6. Missing data

- **Staff roster XLSX** was absent from the package (named in CLIENT_BRIEF).
  → **RESOLVED (client, Q5):** sent separately — now `data/staff_roster_jun2026.xlsx`
  (Jun 2026, 12 outlets, 45 rows; one row per staff per outlet). Shift hours: AM/PM = 7h,
  AM+PM = 13h (1h unpaid break); "TBC" = new hires, use $13. **Confidential — team-only** (Trap T10).
- **4 mall stalls (ORC-01, VIV-01, JEM-01, PGL-01)** have **no daily-summary files** by
  design — their takings run through the mall systems and we have no export; for these the
  `pos_transactions` table is **all there is**. State this limitation wherever a finding
  depends on it (client, Q4).

## 7. Bottom line

The data is *usable* but **not internally consistent**. Before any number is reported
to the client it must pass: outlet dedupe (T1), a single GST basis (T3/T4), invoice
dedupe + amount reconciliation (T5), per-file date parsing (T6), a canonical schema
(T7), and a documented treatment of missing days (T8). **All of these are now resolved
with the client's 18 Nov answers** (see `docs/client_qa.md`), and the build applies the
agreed treatment.

**Data-source authority (client, Q3):** for daily **revenue & volume, the daily-summary
CSVs are authoritative** (Z-report, what Finance uses); `pos_transactions` is for
**product mix / prices / payment mode / time-of-day only** — and is *not* the full
history. For the 4 mall stalls (ORC/VIV/JEM/PGL) the POS table is the **only** source (Q4).
