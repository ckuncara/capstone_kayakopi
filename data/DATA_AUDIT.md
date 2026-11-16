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
> actually contains **69**. Flagged — either one file is missing or the brief is wrong.

## 2. POS transactions (`pos_transactions`)

- 33,059 rows; 12 outlet codes **but 13 distinct `outlet_code` values** because both
  `AMK-03` (1,290 rows) and `AMK-3A` (1,180 rows) appear (see Trap T1).
- `payment_mode` values: `cash`, `card`, `transfer` — no nulls.
- **Price basis is mixed**: for *every* menu item the table contains two unit prices —
  the base (ex-GST) price and exactly `base × 1.35` (9% GST-inclusive). Example item 1
  (Kopi): `1.6` and `2.16` coexist across the same months. ⇒ Revenue computed straight
  from `unit_price × qty` overstates GST-inclusive rows by 9% (Trap T3).
- No `NULL`s in required columns; `tx_id` is a clean surrogate key.

## 3. Supplier purchases (`supplier_purchases`)

- 178 rows; 5 suppliers: SUP-01 Heng Huat Coffee Roasters, SUP-02 Sunrise Bakery
  Supplies, SUP-03 Ban Seng Eggs & Provisions, SUP-04 Mewah Sugar Trading,
  SUP-05 **D'Best Frozen Foods** (apostrophe in name — CSV-escaping hazard).
- All rows carry `gst_treatment = 'ex-GST'`, but the matching **paper invoices are
  GST-inclusive** (see §5) — the same purchase is recorded ex-GST in the table and
  incl-GST on the paper invoice (Trap T4).
- **Duplicate `invoice_no`**: `INV-042509-752` appears twice with different dates
  (05/09/2025 = S$1,135; 11/09/2025 = S$887) (Trap T5).
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
  BDK-01 12, JUR-01 15, TAM-01 17, TPY-02 20, WDL-01 21, YIS-02 20. (Trap T8 — likely
  closure days, but not labelled.)

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
  `M2-1636016-1` and `M3-9325232-5`) — one of them is a typo or a wrong branch (Trap T9).
- All 69 scans matched an `invoice_no` in `supplier_purchases`; 108 DB rows have no scan.

## 6. Missing data

- **Staff roster XLSX is absent** from the package (named in CLIENT_BRIEF). Any
  headcount / payroll question cannot be answered from this data (Trap T10).

## 7. Bottom line

The data is *usable* but **not internally consistent**. Before any number is reported
to the client it must pass: outlet dedupe (T1), a single GST basis (T3/T4), invoice
dedupe + amount reconciliation (T5), per-file date parsing (T6), a canonical schema
(T7), and a documented treatment of missing days (T8). All of these are now tracked in
`docs/data_quality_traps.md`.
