# Team 1 (Kaya Kopi) — FINAL client email (13 consolidated questions)

> **To:** leonard.ngwt@ntu.edu.sg
> **Subject (exact):** `Client question — Team 1`
> **Supersedes:** the 9-question v1 draft below this line (kept for reference).
> **Send early —** answer SLA is 2 working days; latest safe send is 20 Nov (CP1),
> and answers must land before the 27 Nov demo.
> **After sending:** record send in `docs/client_comms.md` (Comms log), set all 13 → `Sent to client`,
> and log each verbatim answer in `docs/client_qa.md` as it arrives.

---

Dear Serene,

Thank you for the data package. We have completed a full audit of every file —
the SQL dump, the eight outlet CSVs, the scanned invoices, and the WhatsApp and
email exports. Before we build on this data, we need to confirm the items below
so our answers are accurate. They are grouped by topic, and we cite the specific
records where relevant so your team can look them up quickly.

**Coverage and completeness**

1. Outlet AMK-03 / AMK-3A. The May 2026 email says the outlet was renamed on 18 May,
   but in the POS data the code switches at the 1 Jan 2026 boundary
   (AMK-03: 1,290 rows; AMK-3A: 1,180 rows). Is AMK-3A the permanent name, what is
   the exact effective date, and should earlier transactions be reported under the
   old code? Note that AMK-3A is absent from the outlet master, and the
   daily-summary files continue to be named AMK-03 through June 2026.

2. Invoice count. The brief lists 70 scanned invoices, but the package contains 69.
   Could you provide the missing scan, or confirm that 69 is the complete set?

3. POS export completeness. Is the SQL export the full transaction history, or a
   sample? For AMK-01 on 01/07/2025, pos_transactions shows 9 transactions (13
   units, S$48.20), while the daily summary reports 263 cups (S$815.51) — a
   difference too large for rounding. Which source should we treat as
   authoritative for daily revenue and volume?

4. Missing outlet summaries. Daily-summary files were provided for eight outlets,
   but the outlet master lists twelve. Could you provide summaries for ORC-01,
   VIV-01, JEM-01 and PGL-01, or confirm that these outlets are intentionally
   excluded from summary reporting?

5. Staff roster. The brief lists a staff-roster XLSX in the package, but it was not
   included. Could you send it, including the meaning of the hours and labour-cost
   fields? We need it to answer the over-staffing and labour-cost questions.

**GST and monetary amounts**

6. GST basis in the POS data. pos_transactions contains both ex-GST and
   GST-inclusive unit prices for the same item (e.g. kopi at S$1.60 and S$2.16).
   Is GST always 9%? On which basis should revenue and margins be reported?

7. Purchase table vs invoices. What exactly does supplier_purchases.total_amount
   represent — pre-GST subtotal, GST-inclusive total, or amount paid? Is the
   "ex-GST" flag reliable for every row? The paper invoices are GST-inclusive and
   their totals do not match the table (e.g. INV-052606-498: invoice S$1,226.00
   vs table S$1,080.00; INV-052507-964: S$599.05 vs S$600.00). You mentioned the
   supplier prices in the system look stale and the scanned invoices are the
   truth — should we treat the paper invoice totals as the source of truth for
   supplier costs?

8. Duplicate invoice number. INV-042509-752 appears twice in the purchase table
   (dated 5 Sep and 11 Sep, different amounts; purchase IDs 5040 and 5041). Is one
   a re-issue of the other, are these two genuine purchases, or should one be
   voided?

9. Supplier GST registration number. Invoices from the same supplier show two
   different GST Reg Nos (e.g. D'Best Frozen Foods: M2-... and M3-...). Which is
   the current one?

**Metric definitions and formats**

10. Daily-summary column definitions. The four file variants use different labels.
    Could you confirm the definitions of "Total Sales" / "sales$" / "REVENUE" /
    "Gross Takings" (do they include GST, discounts, refunds, or delivery-platform
    fees?), and of "No of Cups" / "txns" / "CUPS" / "Customer Count" (are these
    the same measure)?

11. WASTAGE_KG. This field appears only in the BDK-01 and WDL-01 summaries. Is it
    total outlet wastage, ingredient-only, or product-specific? Should the other
    six outlets' summaries contain it as well?

12. Date formats. The TPY-02 and YIS-02 summaries use MM/DD/YYYY dates, while the
    other six files use DD/MM/YYYY. Is this intentional per outlet, or an export
    error?

13. Missing dates in the summaries. Each daily-summary file skips 12–21 days
    within its reporting period (e.g. AMK-01, WDL-01). Do absent dates mean the
    outlet was closed with zero sales, or that the report is missing and should be
    excluded from averages? Also, should TPY-02 have a 30 Jun 2026 record — the
    file currently ends at 29 Jun?

If any of these can be answered by an additional file (the staff roster, the
missing outlet summaries, the missing invoice scan), attaching it would be most
helpful. We would appreciate responses by 20 Nov so we can incorporate them before
our next milestone. We will record your answers in our repository as they arrive.

Thank you for your time and support.

Best regards,
Team 1 (Kaya Kopi)
FDe AI Engineering, FT Cohort 1
