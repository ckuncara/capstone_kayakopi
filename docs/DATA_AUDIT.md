# Team 1 (Kaya Kopi) — Data Audit

Audit of the client data package delivered 16 Nov 2026; inventory and counts
rechecked 20 Nov 2026. Findings reconciled against
[CK's traps](data/docs/data_quality_traps_ck.md) and
[Pran's traps](Pran_Data_Quality_Trap_List.md).
Sources: `sql/kayakopi_pos.sql`, `tabular/*.csv` (8 outlet files),
`images/supplier_invoices/*.jpg` (69 scans), `docs/emails/*`, `docs/whatsapp_exports/*`.
Paths below are relative to `data/` unless explicitly stated otherwise.

> Rule of thumb applied: anything a number *implies* but the row doesn't state is
> flagged, not silently "fixed".

## 1. Sources inventory

The tables in this section, §4 and §5 enumerate every current file under `data/`,
including generated documentation and macOS metadata. Counts exclude CSV headers.
Text files have no intrinsic page count, so line counts are recorded instead.
Repository application/configuration files are outside this client-data inventory.
The newly supplied root-level roster and generated invoice CSV are also included.

| File / table | Type | Rows, pages or lines | Opened / checked | What looks odd |
|---|---|---|---|---|
| `sql/kayakopi_pos.sql` | MySQL SQL dump | 33,388 lines; four tables below | Yes; read and loaded into an in-memory SQLite database after removing MySQL-only directives | Legacy date strings, non-itemized purchases, duplicate identifiers |
| ↳ `outlets` | SQL table | 12 rows | Yes | AMK-03 alias appears as AMK-3A in POS |
| ↳ `menu_items` | SQL table | 20 rows | Yes | Base prices differ from mall prices |
| ↳ `pos_transactions` | SQL table | 33,059 rows | Yes | 13 raw outlet codes; 111 repeated transaction IDs (§2) |
| ↳ `supplier_purchases` | SQL table | 178 rows | Yes | 177 distinct invoice numbers; stale costs and missing scan coverage (§3) |
| `BRIEFING_team1_kaya_kopi.pdf` | PDF | 2 pages | Yes; both pages opened and text extracted | Briefing describes a 12-outlet chain; initial package lacked the roster |
| `CLIENT_BRIEF.md` | Markdown | 52 lines | Yes | Manifest promises 70 scans and a roster XLSX |
| `README.md` | Markdown | 8 lines | Yes | Ingestion instructions, not transactional data |
| `docs/emails/2026-05-18_outlet_rename.txt` | Text email | 11 lines | Yes | Sent 18 May 2026; rename effective 1 Jan 2026 |
| `docs/emails/2026-06-02_finance_query.txt` | Text email | 12 lines | Yes | Reports stale prices and invoice/table discrepancies |
| `docs/whatsapp_exports/ops_chat_export.txt` | Text chat | 162 lines; 160 messages | Yes | Partial, non-chronological export; shortages and delivery issues are reports, not settled adjustments |
| `docs/data_quality_traps_ck.md` | Generated Markdown | N/A rows/pages; T1–T17 | Yes | Reported client findings and unresolved conflicts; not a raw input |
| `.DS_Store` | macOS binary metadata | N/A rows/pages; 10,244 bytes | Yes; bytes read | Exclude from ingestion |
| `images/.DS_Store` | macOS binary metadata | N/A rows/pages; 8,196 bytes | Yes; bytes read | Exclude from ingestion |
| `tabular/.DS_Store` | macOS binary metadata | N/A rows/pages; 6,148 bytes | Yes; bytes read | Exclude from ingestion |
| `../InvoiceData.csv` | Generated CSV, repository root | 136 item rows; 69 invoices; 12 columns | Yes; CSV parsed | Repeated invoice-level totals; two incorrect printed totals (§5) |
| `../staff_roster_jun2026.xlsx` | Excel workbook, repository root | 1 sheet; 46 data rows; header row 3 | Yes; workbook and worksheet XML read | Seven `$13/hr` values and seven `TBC` rates; June-only coverage (§6) |

The eight CSV files are individually inventoried in §4 and all 69 JPEGs in §5.
All available client-data files were opened; all JPEGs were fully decoded.
The prior local OCR work extracted item lines, GST numbers and dates from all
69 scans. Image decoding alone does not establish OCR accuracy.

> `data/CLIENT_BRIEF.md` promises 70 scans; the folder has 69. CK reports a missing
> expense scan, but T2 and T5 disagree about its date. The existing
> `INV-042509-752.jpg` is dated 5 Sept. Keep the expense-to-scan match unresolved
> until supporting evidence identifies the missing expense.

## 2. POS transactions (`pos_transactions`)

- 33,059 rows, covering 2025-07-01 → 2026-06-30; **13 distinct raw outlet codes**
  become **12 outlets** after mapping `AMK-3A` to the same outlet as `AMK-03`
  (1,180 + 1,290 rows). Both trap lists' claim of 11 outlets conflicts with the
  12-row outlet master and the client brief. The email was sent 18 May 2026 but
  explicitly dates the rename to 1 Jan 2026 (T1).
- `payment_mode` values: `CASH`, `GRABPAY`, `NETS`, `PAYNOW`, `VISA`.
- **Mall uplift, not evidence of GST inclusion:** 18,974 kopitiam rows use base
  prices; 14,085 mall rows use `round(base × 1.35, 2)`. Kopi is S$1.60 vs S$2.16.
  CK records client clarification that this reflects higher mall rents (T3).
  Do not divide the uplift by 1.09 or infer tax treatment from it, as Pran's
  proposed handling would do; verify GST treatment separately.
- No `NULL`s in required columns, but **`tx_id` is not unique**: 32,948 distinct
  IDs in 33,059 rows; 111 IDs occur twice. For example, ID `100010` repeats the
  same AMK-03 transaction at `2025-07-01 18:37:43`. Investigate repeats before
  aggregating; neither count all repeats blindly nor silently discard them.

## 3. Supplier purchases (`supplier_purchases`)

- 178 rows; 5 suppliers: SUP-01 Heng Huat Coffee Roasters, SUP-02 Sunrise Bakery
  Supplies, SUP-03 Ban Seng Eggs & Provisions, SUP-04 Mewah Sugar Trading,
  SUP-05 **D'Best Frozen Foods** (apostrophe in name — CSV-escaping hazard).
- All rows carry `gst_treatment = 'ex-GST'`, but the matching **paper invoices are
  GST-inclusive** (see §5) — the same purchase is recorded ex-GST in the table and
  incl-GST on the paper invoice (Trap T4).
- **Duplicate `invoice_no`**: `INV-042509-752` appears twice with different dates
  (05/09/2025 = S$1,135; 11/09/2025 = S$887) (Trap T5).
- Dates stored as `DD/MM/YYYY`, covering **2025-07-02 → 2026-06-28**; purchases
  `5011` and `5016` are dated 02/07/2025.
- Records contain expense totals, not item descriptions, quantities or unit
  prices. CK's calculated itemized data is in `../InvoiceData.csv` (T12).
  Purchase lines must retain source expense IDs; invoice number alone is not a key.
- Stale system costs are explicitly reported in the client brief and finance
  email (T13). Use invoice-date and location-specific prices for reconciliation,
  not one supplier-wide price or retroactive repricing of all history (T14).

## 4. Daily outlet CSVs (8 files)

| File (all CSV) | Rows | Date format | Opened / parsed | What looks odd |
|---|---|---|---|---|
| `tabular/daily_summary_AMK-01.csv` | 344 | DD/MM/YYYY | Yes | Remarks; 21 missing days |
| `tabular/daily_summary_AMK-03.csv` | 344 | YYYY-MM-DD | Yes | `txns` measure; old outlet alias; 21 missing days |
| `tabular/daily_summary_BDK-01.csv` | 353 | DD-MON-YY | Yes | WASTAGE_KG; 12 missing days |
| `tabular/daily_summary_JUR-01.csv` | 350 | DD/MM/YYYY | Yes | Remarks; 15 missing days |
| `tabular/daily_summary_TAM-01.csv` | 348 | YYYY-MM-DD | Yes | `txns` measure; 17 missing days |
| `tabular/daily_summary_TPY-02.csv` | 344 | MM/DD/YYYY | Yes | Weather; 21 missing days; last date 2026-06-29 |
| `tabular/daily_summary_WDL-01.csv` | 344 | DD-MON-YY | Yes | WASTAGE_KG; 21 missing days |
| `tabular/daily_summary_YIS-02.csv` | 345 | MM/DD/YYYY | Yes | Weather; 20 missing days |

- There are **four date formats**, not just the two claimed in both trap lists
  (T6). First-row examples: AMK-01 `01/07/2025`, AMK-03 `2025-07-01`, BDK-01
  `01-JUL-25`, TPY-02 `07/01/2025`. Use a per-file format map.
- Headers are **not uniform** across files: `Date`/`date`/`DAY`/`Business Date`;
  `Total Sales (SGD)`/`sales$`/`REVENUE`/`Gross Takings`; `No of Cups`/`txns`/`CUPS`/
  `Customer Count`. Pran also lists `Qty`/`Quantity`/`QTY`/`Units`, but those
  headers are not in the eight current files. Canonical mapping must preserve
  meaning: transactions/customers are not automatically cups sold (T7).
- **Missing days** within the 2025-07-01→2026-06-30 window: AMK-01 21, AMK-03 21,
  BDK-01 12, JUR-01 15, TAM-01 17, TPY-02 **21**, WDL-01 21, YIS-02 20 (T8).
  All parsed dates are in range with no repeated dates. Closures are only a
  possibility; the absent dates are not labelled, so do not fill them with zero.

## 5. Scanned invoices (69 JPEGs)

All 69 JPEGs opened and decoded; prior local Vision OCR is captured in
`../InvoiceData.csv` (136 item rows, including GST registration numbers and ISO
invoice dates). Findings:
- Every paper invoice is **GST-inclusive**: `Subtotal`, then `GST (9%)`, then `TOTAL`.
  The DB row for the same `invoice_no` stores the **ex-GST** amount only (Trap T4).
- **Amount mismatches** between paper `Subtotal` and the DB row, e.g.:
  - INV-052507-964: paper 599.05 vs DB 600.00
  - INV-052606-498: paper 1226.00 vs DB 1080.00
  - INV-042509-752: paper 1155.36 vs DB 1135.00 (and a second DB row 887.00)
  - INV-032602-477: paper 19.30 vs DB 17.80
- **69 distinct GST numbers and 69 distinct addresses** across 69 invoices (T9).
  For example, D'Best shows `M2-1636016-1` and `M3-9325232-5` at different addresses.
  No address repeats, so this does not establish a typo or whether a recurring
  location keeps the same number. Pran's proposed supplier-wide canonical number
  would lose the invoice/location evidence; confirm legal entities with the client.
- All 69 scan invoice numbers occur in the database. **70 purchase rows** share
  those numbers because one invoice number is duplicated; **108 purchase rows**
  have no matching scan filename. That is identifier coverage, not 70 verified
  expense-to-scan matches.
- Two printed totals are arithmetically incorrect (T15): `INV-012601-458` shows
  S$1,138.79 instead of S$1,142.15; `INV-032512-186` shows S$990.67 instead of
  S$993.22. Preserve printed values separately from calculated totals.
- CK reports location-dependent prices and an eight-location Kopi powder increase
  of 1.11%–12.79%, weighted to 8.1% (T14/T16). The comparison inputs and weights
  have not been independently reproduced in this audit; these are reported
  findings, not verified calculations or automatic justification for a cup-price rise.

### 5.1 Per-image inventory

Every filename below is under `images/supplier_invoices/`; each is a JPEG,
contains **one invoice page**, and **opened successfully (fully decoded)**.
Unless noted, the applicable anomalies are the cross-source amount/price
differences and location-specific GST numbers described above; a dash does not
mean the invoice has been certified correct.

| File | Type | Pages | Opened | Additional anomaly |
|---|---|---|---|---|
| `INV-012508-182.jpg` | JPEG | 1 | Yes | — |
| `INV-012508-224.jpg` | JPEG | 1 | Yes | — |
| `INV-012510-265.jpg` | JPEG | 1 | Yes | — |
| `INV-012510-685.jpg` | JPEG | 1 | Yes | — |
| `INV-012512-270.jpg` | JPEG | 1 | Yes | — |
| `INV-012512-752.jpg` | JPEG | 1 | Yes | — |
| `INV-012512-804.jpg` | JPEG | 1 | Yes | — |
| `INV-012601-458.jpg` | JPEG | 1 | Yes | Printed total S$3.36 below subtotal + GST |
| `INV-012604-874.jpg` | JPEG | 1 | Yes | — |
| `INV-012605-334.jpg` | JPEG | 1 | Yes | — |
| `INV-012605-700.jpg` | JPEG | 1 | Yes | — |
| `INV-012606-440.jpg` | JPEG | 1 | Yes | — |
| `INV-022509-997.jpg` | JPEG | 1 | Yes | — |
| `INV-022510-610.jpg` | JPEG | 1 | Yes | — |
| `INV-022512-134.jpg` | JPEG | 1 | Yes | — |
| `INV-022512-485.jpg` | JPEG | 1 | Yes | — |
| `INV-022601-458.jpg` | JPEG | 1 | Yes | — |
| `INV-022602-556.jpg` | JPEG | 1 | Yes | — |
| `INV-022602-661.jpg` | JPEG | 1 | Yes | — |
| `INV-022602-845.jpg` | JPEG | 1 | Yes | — |
| `INV-022605-880.jpg` | JPEG | 1 | Yes | — |
| `INV-032507-553.jpg` | JPEG | 1 | Yes | — |
| `INV-032507-782.jpg` | JPEG | 1 | Yes | — |
| `INV-032508-243.jpg` | JPEG | 1 | Yes | — |
| `INV-032509-689.jpg` | JPEG | 1 | Yes | — |
| `INV-032510-393.jpg` | JPEG | 1 | Yes | — |
| `INV-032512-186.jpg` | JPEG | 1 | Yes | Printed total S$2.55 below subtotal + GST |
| `INV-032512-744.jpg` | JPEG | 1 | Yes | — |
| `INV-032601-323.jpg` | JPEG | 1 | Yes | — |
| `INV-032602-259.jpg` | JPEG | 1 | Yes | — |
| `INV-032602-477.jpg` | JPEG | 1 | Yes | Paper subtotal S$19.30 vs DB S$17.80 |
| `INV-032603-485.jpg` | JPEG | 1 | Yes | — |
| `INV-032603-511.jpg` | JPEG | 1 | Yes | — |
| `INV-032604-246.jpg` | JPEG | 1 | Yes | — |
| `INV-032605-108.jpg` | JPEG | 1 | Yes | — |
| `INV-032605-344.jpg` | JPEG | 1 | Yes | — |
| `INV-032605-642.jpg` | JPEG | 1 | Yes | — |
| `INV-032606-186.jpg` | JPEG | 1 | Yes | — |
| `INV-032606-948.jpg` | JPEG | 1 | Yes | — |
| `INV-042507-483.jpg` | JPEG | 1 | Yes | — |
| `INV-042508-872.jpg` | JPEG | 1 | Yes | — |
| `INV-042509-653.jpg` | JPEG | 1 | Yes | — |
| `INV-042509-752.jpg` | JPEG | 1 | Yes | Dated 5 Sept; two DB expenses share its number |
| `INV-042511-555.jpg` | JPEG | 1 | Yes | — |
| `INV-042511-576.jpg` | JPEG | 1 | Yes | — |
| `INV-042512-347.jpg` | JPEG | 1 | Yes | — |
| `INV-042601-609.jpg` | JPEG | 1 | Yes | — |
| `INV-042603-319.jpg` | JPEG | 1 | Yes | — |
| `INV-042604-885.jpg` | JPEG | 1 | Yes | — |
| `INV-042604-977.jpg` | JPEG | 1 | Yes | — |
| `INV-042605-444.jpg` | JPEG | 1 | Yes | — |
| `INV-042606-154.jpg` | JPEG | 1 | Yes | — |
| `INV-042606-890.jpg` | JPEG | 1 | Yes | — |
| `INV-052507-964.jpg` | JPEG | 1 | Yes | Paper subtotal S$599.05 vs DB S$600.00 |
| `INV-052508-488.jpg` | JPEG | 1 | Yes | — |
| `INV-052508-666.jpg` | JPEG | 1 | Yes | — |
| `INV-052509-303.jpg` | JPEG | 1 | Yes | — |
| `INV-052510-154.jpg` | JPEG | 1 | Yes | — |
| `INV-052511-687.jpg` | JPEG | 1 | Yes | — |
| `INV-052512-140.jpg` | JPEG | 1 | Yes | — |
| `INV-052601-745.jpg` | JPEG | 1 | Yes | — |
| `INV-052601-941.jpg` | JPEG | 1 | Yes | — |
| `INV-052602-640.jpg` | JPEG | 1 | Yes | — |
| `INV-052602-789.jpg` | JPEG | 1 | Yes | — |
| `INV-052603-299.jpg` | JPEG | 1 | Yes | — |
| `INV-052604-328.jpg` | JPEG | 1 | Yes | — |
| `INV-052604-696.jpg` | JPEG | 1 | Yes | — |
| `INV-052605-997.jpg` | JPEG | 1 | Yes | — |
| `INV-052606-498.jpg` | JPEG | 1 | Yes | Paper subtotal S$1,226.00 vs DB S$1,080.00 |

## 6. Missing data

- **The initially missing roster is now supplied:** `../staff_roster_jun2026.xlsx`
  opened successfully. Its one sheet, `Roster Jun 2026`, has a title in row 1,
  header in row 3, and **46 data rows (4–49)** across columns A–K. Count excludes
  the title, blank row and header.
- Pran's mixed-rate finding is verified (CK T17): column K contains numeric
  `15` (15 rows), `13` (7), `14` (7), `12` (3), text `$13/hr` (7), and `TBC` (7).
  Text-rate examples are K6 (`$13/hr`) and K10 (`TBC`). Unresolved cells are
  K10, K15, K23, K27, K28, K43 and K48.
- Normalize confirmed rates to numeric SGD/hour, but retain `TBC` as unknown,
  not zero. Verify unique staff and worked hours before payroll calculations;
  46 roster rows are not automatically 46 employees. The June 2026 roster alone
  cannot answer historical staffing questions for the entire POS period.
- Roster entries contain personal staff information; avoid exposing names in
  aggregate audit findings.

## 7. Bottom line

The data is *usable* but **not internally consistent**. Before any number is reported
to the client it must pass: outlet alias resolution (T1), separate mall pricing
and GST treatment (T3/T4), transaction-ID investigation, expense identity and amount
reconciliation (T5/T12–T15), per-file date parsing (T6), a meaning-preserving schema
(T7), documented missing-day treatment (T8), invoice/location GST attribution (T9),
and verified roster inputs for labour costs (T10/T17).
CK and Pran overlap on all earlier traps; only Pran's mixed roster-rate issue was
added as T17. Their conflicting claims are explicitly qualified above rather than
silently used as audit evidence.
