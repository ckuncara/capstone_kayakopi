# Teammate's Question Tracker — as received 18 Nov (PDF: "Question Tracker - Sheet1")

> Archived verbatim for the comms record. Merged into `client_comms.md` intake v2
> (their Q1→#1, Q2→#2, Q3→#3, Q4→#5, Q5→#10, Q6→#11, Q7→#7, Q8→#6, Q9→#12, Q10→#2).

## Q1 — Is the POS transaction export complete, and which source should govern daily sales?
Is the SQL export a sample or otherwise incomplete? For daily revenue and volume,
should we treat the outlet summary files or the POS transactions as authoritative?
The SQL has 9 physical records on that outlet-date, totalling 13 units and SGD 48.20,
while the daily summary reports SGD 815.51 and 263 cups. The scale difference is too
large to explain as rounding or a date-format issue.
Source: `data/sql/kayakopi_pos.sql`, `pos_transactions`, outlet AMK-01 dated 2025-07-01
(lines 72–80; tx IDs 100001–100009); `data/tabular/daily_summary_AMK-01.csv` record
01/07/2025 (line 2), Total Sales (SGD) / No of Cups.

## Q2 — Are the four outlets without daily-summary files intentionally excluded?
We received daily-summary files for eight outlets, but the outlet master lists twelve.
Should daily summaries also exist for ORC-01, VIV-01, JEM-01, and PGL-01? If so,
please provide them; if not, confirm that outlet-level summary comparisons must
exclude those four outlets.

## Q3 — Does AMK-03 become AMK-3A from 1 January 2026, including in the CSV?
Confirm whether POS code AMK-3A is the same physical outlet as AMK-03 from
1 January 2026 onward, and whether rows still labelled AMK-03 in the daily-summary
filename after that date belong to AMK-3A. AMK-3A is absent from the outlet master
but replaces AMK-03 in POS rows at the calendar-year boundary. The CSV continues
under an AMK-03 filename through June 2026.
SQL: `SELECT tx_id, outlet_code, tx_time FROM pos_transactions WHERE outlet_code IN
('AMK-03','AMK-3A') AND tx_time >= '2025-12-31' AND tx_time < '2026-01-02'
ORDER BY tx_time, tx_id;`

## Q4 — Do missing CSV dates mean closure/no trade, or missing reports?
Each daily-summary file skips some dates within its reporting period. Should an
absent date mean the outlet was closed with zero sales, or that the report is
missing/unknown? Also confirm whether TPY-02 should have a 30 June 2026 record.
Source: `daily_summary_TAM-01.csv` 2025-07-02 & 2025-07-04 (absent 07-03);
`daily_summary_AMK-03.csv` 2026-01-13 & 2026-01-15 (absent 01-14);
`daily_summary_TPY-02.csv` final record 06/29/2026 (line 345).

## Q5 — What do the different daily-summary measures mean, and are they comparable?
Define "Total Sales", "sales$", "REVENUE", "Gross Takings" (incl. GST, discounts,
refunds, delivery-platform fees). Are "No of Cups", "txns", "CUPS", "Customer
Count" the same thing? Four distinct schemas use different labels; the SQL records
item quantity incl. food as well as drinks.
Source: header rows of daily_summary_AMK-01/AMK-03/BDK-01/TPY-02 (line 1);
`kayakopi_pos.sql` `pos_transactions` cols qty, unit_price, total (lines 61–69).

## Q6 — What is the definition and expected coverage of wastage?
Is WASTAGE_KG total outlet wastage, ingredient-only, or product-category? Should the
other six outlet summaries also contain wastage data? The "worst wastage" comparison
cannot be generalized from two outlets, and kg cannot be translated to financial loss
without a definition.
Source: WASTAGE_KG in BDK-01 & WDL-01 (line 1); absent in the other six;
`CLIENT_BRIEF.md` lines 11–14.

## Q7 — Which of the two conflicting records for invoice INV-042509-752 is valid?
Same invoice number twice with different dates, amounts, purchase IDs (5040 & 5041,
lines 33248–33249). Dump declares purchase_id (not invoice_no) as PK, so both load.
Duplicate/correction, two genuine purchases, or void one?
Source: `supplier_purchases`, cols purchase_date, invoice_no, total_amount.

## Q8 — What exactly does supplier_purchases.total_amount represent?
Pre-GST subtotal, GST-inclusive, amount paid, or other? Is `gst_treatment='ex-GST'`
reliable for every row? How are credits/adjustments represented? No GST amount,
subtotal, paid amount, or credit-note marker exists. Brief says Finance cannot
reconcile this table to invoice totals.
Source: `kayakopi_pos.sql` lines 1–3 & 33199–33207; all purchases labelled ex-GST;
`CLIENT_BRIEF.md` lines 16–19.

## Q9 — Please provide the staff-roster spreadsheet or confirm it is unavailable.
Manifest mentions a staff-roster XLSX; no roster file present. Provide it (incl.
hours & labour-cost field meanings) or confirm labour/overstaffing questions are out
of scope.
Source: `CLIENT_BRIEF.md` lines 11–14 & 21–22; `data/tabular/` has 8 CSVs, no XLSX.

## Q10 — Why are there only 69 invoice scans instead of 70?
Brief says 70 scans in `images/supplier_invoices/`; only 69 found. Provide the
missing scan or confirm only 69 exist.
