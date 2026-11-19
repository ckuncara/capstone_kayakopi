# Client Comms Log — Team 1 (Kaya Kopi)

> **Single source of truth** for all client communication.
> **Comms owner (POC):** Mike Yap
> **Rule:** every exchange with the client is logged here the same day it happens.
> Client **answers** are also recorded in `docs/client_qa.md` (README rule 5).
> This file is the *tracker*; `client_qa.md` is the *verbatim answer log*.

---

## Client contact & SLA

| | |
|---|---|
| Client | Serene |
| Email channel | leonard.ngwt@ntu.edu.sg (per course instruction) |
| Email subject (exact) | `Client question — Team 1` |
| Response SLA | 2 working days — send early enough to land before deadlines |
| Deadline pressure | CP1 Fri 20 Nov 17:00 · CP2 Mon 23 Nov 09:00 · Demo Fri 27 Nov 09:00 |

---

## Workflow (how this runs)

1. **Team** sends questions to the POC (format in *Intake* below).
2. **POC** dedupes, merges, and batches them with any open items.
3. **POC** emails the client (subject line above), one consolidated message.
4. **Log** the send in the *Comms log* table; set each question → `Sent to client`.
5. **When answer arrives:** record verbatim in `client_qa.md`, update the *Intake* row → `Answered`, add the send/answer to the *Comms log*, and note the **impact** on the build.

---

## Comms log (every exchange, newest last)

| Date | Dir | Channel | Subject | Summary | Follow-up / owner |
|---|---|---|---|---|---|
| 18 Nov | In | Telegram | Teammate's question tracker | 10-question tracker (Q1–Q10) received from teammate, w/ SQL citations | Merged into intake — Mike |
| 17 Nov | T→C | Email (drafted) | Client question — Team 1 | 9 data-audit questions (v1) prepared in `client_email_draft.md` | Merged to v2 (13 Qs) — Mike |
| 18 Nov | C→T | Email | Client question — Team 1 (RE) | **Client reply received**: Q1 answered (AMK-3A = 1 Jan 2026, alias of AMK-03) · Q2 answered (69 = complete) · Q3 **cut off in relay** ("3. POS export completeness. It") · roster attached | Log full reply in `client_qa.md` — Mike ⚠️ **rest of email (Q3–Q13) still missing** |
| 18 Nov | C→T | Email attach | — | **Staff roster received**: `staff_roster_jun2026.xlsx` → saved `data/staff_roster_jun2026.xlsx` (Jun 2026, 12 outlets, 45 staff rows; 4 rates "TBC") | Unblocks Q12 / over-staffing answers |

---

## Team question intake — CONSOLIDATED v2 (13 questions)

> Sources: **M** = Mike's 16 Nov audit draft · **T** = teammate's tracker (18 Nov) · both = merged.
> **Still pending:** questions from remaining teammates — collect by Thu 19 Nov, then finalize email.

| # | Question | Src | Why it matters | Status |
|---|---|---|---|---|
| 1 | AMK-03 → AMK-3A rename: permanent? ⚠️ **date conflict:** our audit says 18 May 2026 (May email); teammate's data shows the code switches at the **1 Jan 2026** boundary in POS rows; AMK-3A absent from outlet master; CSV filename stays AMK-03 through Jun 2026. Ask: exact effective date + report old rows under which code? | M+T | Outlet canonicalisation (T1) | **Answered 18 Nov** — 1 Jan 2026, alias of AMK-03 |
| 2 | Brief says 70 scanned invoices; package has 69. Which is missing, or is 70 a typo? | M+T | Invoice completeness claim (T2) | **Answered 18 Nov** — 69 is complete; 70 = hardcopies pulled (dup invoice no.), no 70th file |
| 3 | **NEW** POS export completeness: is the SQL export a sample? For daily revenue/volume, which is authoritative — `pos_transactions` or the daily-summary CSVs? (AMK-01 2025-07-01: SQL 9 txns/13 units/S$48.20 vs summary S$815.51/263 cups.) | T | Data-source authority | **Answered 18 Nov (pt 3)** — summaries (Z-report) = authoritative for revenue & volume; POS table = product mix / prices / payment mode / time-of-day; report POS gaps |
| 4 | `pos_transactions` mixes ex-GST & GST-inclusive unit prices (Kopi $1.60 vs $2.16). Is GST always 9%? Which basis should reported revenue use? | M | Revenue normalisation (T3) | **Answered 18 Nov (pt 6)** — NOT a GST effect: $1.60 kopitiam vs $2.16 mall price (+35%); all POS prices GST-incl; 9% GST throughout; report revenue as takings |
| 5 | **NEW** Daily-summary files exist for 8 outlets; outlet master lists 12. Are summaries for ORC-01, VIV-01, JEM-01, PGL-01 missing (please provide) or intentionally absent (exclude from comparisons)? | T | Coverage of outlet comparisons | **Answered 18 Nov (pt 4)** — intentional: 4 mall stalls keep no own sheets (mall rent systems, no export); POS table is all there is for them; state the limitation |
| 6 | `supplier_purchases.total_amount`: pre-GST subtotal, GST-inclusive, or amount paid? Is the `ex-GST` flag reliable per row? Paper invoices differ from table (INV-052606-498: S$1,226.00 vs S$1,080.00) — which is source of truth for supplier costs? | M+T | Cost reconciliation (T4/T5) | **Answered 18 Nov (pt 7)** — total_amount = stale ex-GST subtotal; flag right (all ex-GST); will never tally with hardcopy; **paper invoice = source of truth**; lines > printed total; flag bad invoices |
| 7 | `INV-042509-752` appears twice (05/09 & 11/09, different amounts; purchase IDs 5040 & 5041) — re-issue, two purchases, or void one? | M+T | Supplier spend (T5) | **Answered 18 Nov (pt 8)** — two genuine deliveries; Mewah reused the number (their mistake); keep both; one hardcopy → match by amount+date, mark the other 'no scan' |
| 8 | TPY-02 & YIS-02 CSVs use MM/DD/YYYY; other six use DD/MM — intentional per outlet, or export bug? | M | Date parsing (T6) | **Answered 18 Nov (pt 12)** — export error / two managers' Excel settings; TPY-02 & YIS-02 = month-first, all others day-first; read as-is, do not ask managers to change files |
| 9 | Missing CSV dates (12–21 days/outlet): planned closures → zero sales, or missing reports → exclude from averages? Also: should TPY-02 have a 30 Jun 2026 record? | M+T | Averages (T8) | **Answered 18 Nov (pt 13)** — missing ≠ closed (closures only CNY days 1–2); unfiled → exclude from averages, show as gap; TPY-02 30 Jun = Kumar did not file |
| 10 | **NEW** Define daily-summary measures across the 4 schema variants: "Total Sales" vs "sales$" vs "REVENUE" vs "Gross Takings" (GST/discounts/refunds/platform fees included?) and "No of Cups" vs "txns" vs "CUPS" vs "Customer Count" — same thing? | T | Metric definitions | **Answered 18 Nov (pt 10)** — all 4 sales cols = gross takings (GST-incl, after refunds, no platform fees); all 4 count cols = receipts; "cups" = receipts not drinks |
| 11 | **NEW** WASTAGE_KG (only in BDK-01 & WDL-01): total outlet wastage, ingredient-only, or product-specific? Should the other 6 outlets have it? Needed for the "worst wastage" comparison. | T | Wastage analysis | **Answered 18 Nov (pt 11)** — daily bin weight, not product-specific; only BDK-01 (Faridah) & WDL-01 record it; others = "no data" (do not impute); WhatsApp export = only other evidence |
| 12 | Staff roster XLSX listed in brief but not in package — please send (incl. hours & labour-cost field meanings), or confirm labour/over-staffing questions are out of scope. | M+T | Headcount answers (T10) | **Answered 18 Nov** — roster received → `data/staff_roster_jun2026.xlsx` |
| 13 | Same supplier, two GST Reg Nos on invoices (D'Best: M2-… vs M3-…) — which is correct? | M | Compliance citations (T9) | **Answered 18 Nov (pt 9)** — client won't guess; identify by **supplier code, not GST no**; list the mismatched numbers for Finance to query |

> **Status values:** `Consolidated` → `Sent to client` → `Answered` (or `Clarify needed` / `Won't fix`).

---

## Answers received (pointer)

> Verbatim answers live in `docs/client_qa.md`. List here only: `# — short answer — date`.

- Q1 — AMK-3A = alias of AMK-03, effective **1 Jan 2026**; one outlet, one label, whole year — 18 Nov (pt 1)
- Q2 — **69 invoices is the complete set**; 70 = hardcopies pulled, dup invoice no., no 70th file — 18 Nov (pt 2)
- Q3 — POS export = **not full history**; summaries (Z-report) authoritative for revenue/volume; POS table = mix/prices/payment/time-of-day; report POS gaps — 18 Nov (pt 3)
- Q4 — $1.60 vs $2.16 = **kopitiam vs mall price** (+35%), not GST; all POS prices GST-incl; **9% GST** throughout; revenue = takings — 18 Nov (pt 6)
- Q5 — 4 mall stalls (ORC/VIV/JEM/PGL) have **no summaries** (intentional; mall rent systems); POS table is all there is — 18 Nov (pt 4)
- Q6 — `total_amount` = **stale ex-GST**; **paper invoice = source of truth**; lines > printed total; flag bad invoices — 18 Nov (pt 7)
- Q7 — INV-042509-752 = **two genuine deliveries**; Mewah reused the number; keep both; match hardcopy by amount+date, other = 'no scan' — 18 Nov (pt 8)
- Q12 — staff roster delivered → `data/staff_roster_jun2026.xlsx`; AM/PM=7h, AM+PM=13h; TBC→$13; team-only file — 18 Nov (pt 5)
- Q13 — supplier identity by **supplier code, not GST no**; list mismatched GST numbers for Finance — 18 Nov (pt 9)
- Q8 — TPY-02 & YIS-02 = month-first (two managers' Excel settings); all others day-first; read as-is, don't ask managers to change files — 18 Nov (pt 12)
- Q9 — missing ≠ closed (closures only CNY days 1–2); unfiled → exclude from averages, show as gap; TPY-02 30 Jun = Kumar did not file — 18 Nov (pt 13)
- Q10 — all 4 sales cols = gross takings (GST-incl, after refunds, no platform fees); all 4 count cols = **receipts**; "cups" = receipts, not drinks — 18 Nov (pt 10)
- Q11 — WASTAGE_KG = daily outlet-wide bin weight, not product-specific; only BDK-01 (Faridah) & WDL-01 record; others = "no data" (don't impute) — 18 Nov (pt 11)
- ✅ **All 13 questions answered (18 Nov)** — reply signed **Serene Lim, Finance & Admin Manager, Kaya Kopi Holdings**
- 📌 **New request (Serene Lim, 18 Nov):** cost trend from the invoices for the **10-cent kopi question** — ready before **demo day (27 Nov)**; build from paper invoices (source of truth per pt 7), not `total_amount`

---

## Open items / blockers

- **Client Q&A: all 13 answered (18 Nov)** — full rules logged in `client_qa.md` (verbatim). No further client questions outstanding.
- **10-cent kopi cost trend (Serene Lim) — ✅ BUILT (19 Nov)** — `build/out/kopi_trend_report.md` + `kopi_trend.csv`. Built from Heng Huat (SUP-01) invoice line items (source of truth per pt 7). **Kopi/roast input S$13.18→S$13.90/kg (+5.5%) and tea dust S$9.68→S$10.92/kg (+12.9%) over 2025-08→2026-06 — both rising**, but both are a small fraction of a $1.60 cup, so the 10-cent margin is dominated by milk/packaging/labour/rent. Caveat: only 7/12 Heng Huat scans yielded a legible unit price (scan-quality gap) — treat as directional. **Show to Serene before demo (27 Nov).**
- **Labour-cost analysis** — roster is **June 2026 only** (not full year) and 4 rates are "TBC" (use $13 per client). Scope any labour/over-staffing finding to June 2026 or state the limit.
- **New client rules to apply in the build** (18 Nov, pts 1–13):
  - AMK-3A → alias of AMK-03, one label all year (effective 1 Jan 2026); outlet master & CSV filenames stay as-is.
  - Invoice completeness = **69** (no 70th file).
  - **Revenue & volume: daily-summary CSVs are authoritative**; `pos_transactions` only for product mix, prices, payment modes, time-of-day; flag POS gaps vs summaries.
  - ORC-01 / VIV-01 / JEM-01 / PGL-01: **no summaries exist** — POS table is sole source; state the limitation in any finding that uses them.
  - GST: **9% throughout**; POS prices are GST-inclusive customer-paid; report revenue as **takings**; mall vs kopitiam is a +35% price tier, not a GST effect; do ex-GST margin arithmetic once and label it.
  - **Supplier cost: paper invoices are source of truth**; DB `total_amount` is stale ex-GST — do not reconcile to it; trust invoice **lines** over printed totals; flag the invoices where lines ≠ total and report them.
  - INV-042509-752: keep **both** purchases (Mewah reused number); match the one hardcopy by amount+date; tag the other 'no scan'.
  - Supplier joins on **supplier code, never GST reg no**; compile the mismatched GST-number list for Finance.
  - **Dates: TPY-02 & YIS-02 are month-first** (managers' Excel settings); all other outlets day-first; parse per-outlet, do NOT ask managers to change files.
  - **Missing CSV dates = unfiled, not closed** (only CNY days 1–2 are closures); exclude from averages, display as gaps; TPY-02 30 Jun 2026 = Kumar did not file.
  - **Metric names: 4 sales cols = gross takings** (GST-incl, after refunds, no platform fees/fees — no delivery platforms); **4 count cols = receipts** ("cups" is kopitiam habit for receipts, not drinks).
  - **WASTAGE_KG = daily outlet-wide bin weight** (not product-specific); only BDK-01 (Faridah) & WDL-01 record it — compare these two only; all other outlets = "no data", never impute; WhatsApp export is the only other evidence.
  - Staff roster = **team-only confidential file**; shifts AM/PM = 7h, AM+PM = 13h paid (1h unpaid break); TBC rate → $13/hr; dual-outlet rows = split week.

---

## Team intake template (copy-paste to send to teammates)

```
Hi team — I'm handling all client comms for the capstone.

We've consolidated 13 questions so far (see docs/client_comms.md). If you have
anything else for the client, send it to me by Thu 18 Nov in this format:

  Q:   <what you need to know>
  Why: <which part of our build it affects>

I'll dedupe, send one batch to the client, and log every answer in
docs/client_comms.md + docs/client_qa.md.
Don't email the client directly — route through me so we ask once, clearly.
```
