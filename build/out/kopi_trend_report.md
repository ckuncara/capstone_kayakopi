# 10-cent Kopi — Cost Trend (from invoices)

Source: scanned Heng Huat Coffee Roasters invoices (SUP-01), the kopi/tea input supplier.
Unit price per kg of the roasted kopi/tea input, over 2025-08 → 2026-06.

Invoices parsed: 69 | DB purchase rows: 178 | DB↔scan reconcilable: 70 | exact subtotal matches: 0

## Input unit price (S$ per kg)

### Kopi (powder/roasted)
- 22/12/2025  S$13.18/kg   [ok]  (INV-012512-752 · Kopi powder 5kg)
- 11/05/2026  S$13.99/kg   [ok]  (INV-012605-700 · Kopi powder 5kg)
- 12/06/2026  S$13.90/kg   [ok]  (INV-012606-440 · Kopi powder 5kg)
  range S$13.18–S$13.99/kg  (Δ +6.1%)  n=3

### Tea (dust)
- 06/08/2025  S$9.83/kg   [ok]  (INV-012508-224 · Tea dust 5kg)
- 20/08/2025  S$9.68/kg   [ok]  (INV-012508-182 · Tea dust 5kg |g)
- 06/12/2025  S$10.34/kg   [ok]  (INV-012512-804 · Tea dust 5kg)
- 12/06/2026  S$10.92/kg   [ok]  (INV-012606-440 · Tea dust 5kg)
  range S$9.68–S$10.92/kg  (Δ +12.9%)  n=4

## Reconciliation: paper invoices vs DB `supplier_purchases`

- **69 of 69 scanned invoices have a DB row** (0 scans missing from the ledger; 108 DB rows have no scan in the package).
- Exact subtotal matches: **0/70** — i.e. the DB `total_amount` is systematically stale. Where both values exist, DB/invoice-subtotal median ratio = **0.929** (DB is lower, consistent with stale ex-GST figures per client Q7).
- Per client Q7: the **paper invoice line items are the source of truth**; the DB amounts are used only as a baseline for matching and are flagged, not trusted.
- One DB invoice number is reused (Mewah `INV-042509-752`, client Q8): both rows are kept and distinguished by amount/date.

## Coverage & caveats (read before citing these numbers)

- Only **7 of 12 Heng Huat invoices** yielded a legible kopi/tea unit price; the rest of the SUP-01 scans have line items that are illegible in the scan itself (a scan-quality gap, not a parser failure).
- Sample is thin: kopi powder n=3, tea dust n=4 across 2025-08→2026-06. Treat the trend as directional, not a precise index.
- **Bottom line for the 10-cent kopi:** kopi input price is **rising** (S$13.18→S$13.99/kg, +6.1% over the window); tea dust is **rising** (S$9.68→S$10.92/kg, +12.9%). The roasted kopi is only a small fraction of a $1.60 cup's cost, so the 10-cent margin question is dominated by milk, packaging, labour and rent — not the kopi input — consistent with the client's '10 cents' framing.
- Rows that could not be resolved from the scan are omitted, not estimated.

> Confidence: `ok` = clean OCR; `count_guessed`/`decimal_recovered` = recovered from line_total÷unit_price (flagged, still reliable for the unit price).
