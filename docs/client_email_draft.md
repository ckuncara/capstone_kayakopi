# Team 1 (Kaya Kopi) — Draft email to client

> **Send from your own email** (not from the course account if you use a different one),
> to **leonard.ngwt@ntu.edu.sg**, subject exactly:
> **`Client question — Team 1`**
> Allow two working days for the answer; record the reply in `docs/client_qa.md`.

---

Dear Serene,

Thank you for the data package. We have completed a full audit of every file
(SQL dump, the 8 outlet CSVs, the scanned invoices, and the WhatsApp/email
exports). Before we build on this, we need to confirm the following so our
answers are accurate.

1. **Outlet AMK-03 / AMK-3A.** The May 2026 email says the outlet was renamed
   18 May, but both codes appear in the POS data (AMK-03: 1,290 rows; AMK-3A:
   1,180 rows). Is AMK-3A the permanent name, and should figures before 18 May
   be reported under the old code?

2. **Invoice count.** The brief lists 70 scanned invoices; the package contains
   69. Could you confirm which one is missing (or advise if 70 was a typo)?

3. **GST basis in the POS data.** `pos_transactions` contains both ex-GST and
   GST-inclusive unit prices for the same items (e.g. Kopi at $1.60 and $2.16).
   Which basis should we use when reporting revenue and margins?

4. **Purchase table vs invoices.** The purchase table records amounts ex-GST,
   but the paper invoices are GST-inclusive and their totals do not match the
   table (e.g. INV-052606-498: invoice total S$1,226.00 vs table S$1,080.00;
   INV-052507-964: S$599.05 vs S$600.00). You mentioned the supplier prices in
   the system look stale and the scanned invoices are the truth — should we
   treat the paper invoice totals as the source of truth for supplier costs?

5. **Duplicate invoice number.** INV-042509-752 appears twice in the purchase
   table (5 Sep and 11 Sep, different amounts). Is one a re-issue of the other?

6. **Date formats in the outlet CSVs.** TPY-02 and YIS-02 use US-style
   MM/DD/YYYY dates; the other six files use DD/MM/YYYY. Was that intentional
   per outlet, or an export error?

7. **Missing days.** Several outlet files have 12–21 days missing within the
   12-month window (e.g. AMK-01, WDL-01). Were these planned closures? Should
   they be treated as zero sales or excluded from averages?

8. **Staff roster.** The brief lists a staff roster XLSX in the package, but it
   was not included. Could you send it? We need it to answer the over-staffing
   and labour-cost questions.

9. **Supplier GST registration number.** Invoices from the same supplier show
   two different GST Reg Nos (e.g. D'Best Frozen Foods: M2-… and M3-…). Which
   is correct?

We will record your answers in our repository as they arrive.

Best regards,
Team 1

---

## After sending

- Update `docs/client_qa.md`: change the date on each question to the send date
  and add a "raised by email 20 Nov" note (answers remain _pending_ until the
  client replies).
- CP1 box 4 needs the email **sent** (answer recorded when it arrives — two
  working days, so a 20 Nov send is the latest that still lands before the
  27 Nov demo; send it today).
