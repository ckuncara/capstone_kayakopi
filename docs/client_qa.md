# Client Q&A (keep every answer here — README rule 5)

> Format: date · question → answer (verbatim) · impact on our build

## Log

### Questions to ask the client (raised from data audit, 16 Nov)

| Date | Question (raised by us) | Answer | Impact |
|---|---|---|---|
| 16 Nov | AMK-03 → AMK-3A: is the 18 May 2026 rename permanent, and should pre-18-May rows be reported under the old name? | _pending_ | T1 — outlet canonicalisation rule |
| 16 Nov | Brief says 70 scanned invoices; folder has 69. Which one is missing, or is the brief wrong? | _pending_ | T2 — invoice completeness claim |
| 16 Nov | `pos_transactions` mixes ex-GST and GST-inclusive unit prices. Is GST always 9%, and which basis should reported revenue use? | _pending_ | T3 — revenue normalisation |
| 16 Nov | `supplier_purchases` stores ex-GST but paper invoices are GST-inclusive with different totals (e.g. INV-052606-498: paper 1226.00 vs DB 1080.00). Which is the source of truth? | _pending_ | T4/T5 — cost reconciliation |
| 16 Nov | `INV-042509-752` appears twice (05/09 and 11/09, different amounts) — one is a re-issue or an error? | _pending_ | T5 — supplier spend |
| 16 Nov | TPY-02 and YIS-02 CSVs use MM/DD dates, the rest DD/MM — intentional, or an export bug? | _pending_ | T6 — date parsing |
| 16 Nov | 12–21 missing days per outlet in the CSVs — planned closures? Should they be zero-filled or excluded from averages? | _pending_ | T8 — averages |
| 16 Nov | Staff roster XLSX was listed in the brief but not in the package — when will it be sent? | _pending_ | T10 — headcount answers |
| 16 Nov | Same supplier shows two GST Reg Nos on invoices (e.g. D'Best M2-… vs M3-…) — which is correct? | _pending_ | T9 — compliance citations |
