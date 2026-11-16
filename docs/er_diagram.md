# Team 1 (Kaya Kopi) — ER diagram (cleaned target schema)

Mermaid (renders on GitHub / Mermaid Live Editor). This is the **target** schema we
propose after cleaning; deltas from the raw `kayakopi_pos.sql` are noted below.

```mermaid
erDiagram
    OUTLET {
        string outlet_code PK "canonical code (AMK-3A, not AMK-03)"
        string name
        string region
        string format "standard|flagship"
        date opened_on
    }
    MENU_ITEM {
        int item_id PK
        string name
        decimal base_price "ex-GST, canonical"
        string category
    }
    TRANSACTION {
        int tx_id PK
        string outlet_code FK
        datetime sold_at
        int item_id FK
        int qty
        decimal unit_price "ex-GST (normalised)"
        decimal gst_amount "9% of subtotal (new col)"
        decimal net_amount "ex-GST subtotal"
        string payment_mode
    }
    SUPPLIER {
        string supplier_code PK
        string name "escape apostrophes"
        string gst_reg_no "canonical single value"
        string contact
    }
    PURCHASE {
        string purchase_id PK "new, to break invoice_no dup"
        string supplier_code FK
        date purchase_date
        string invoice_no "kept, now non-unique"
        decimal amount_ex_gst
        string gst_treatment
        string scan_ref "FK to INVOICE_SCAN (optional)"
    }
    INVOICE_SCAN {
        string invoice_no PK "one per physical paper invoice"
        string supplier_code FK
        date paper_date
        decimal paper_subtotal "ex-GST"
        decimal paper_gst "9%"
        decimal paper_total "incl-GST"
        string file_path
        int ocr_confidence
    }
    DAILY_SUMMARY {
        string outlet_code FK
        date as_of
        decimal sales "canonical, ex-GST"
        int cups
        string weather "nullable"
        decimal wastage_kg "nullable"
    }
    OUTLET ||--o{ TRANSACTION : "sells"
    MENU_ITEM ||--o{ TRANSACTION : "in"
    SUPPLIER ||--o{ PURCHASE : "supplies"
    SUPPLIER ||--o{ INVOICE_SCAN : "billed"
    INVOICE_SCAN ||--o{ PURCHASE : "evidences"
    OUTLET ||--o{ DAILY_SUMMARY : "reports"
```

## Deltas vs the raw schema (what cleaning changes)

1. **`outlet_code` canonicalised** — `AMK-03` rows are remapped to `AMK-3A` (renamed
   18 May 2026, per email `2026-05-18_outlet_rename.txt`). Raw data keeps both codes.
2. **`TRANSACTION.unit_price` forced ex-GST** — GST-inclusive rows (`base × 1.35`) are
   divided by 1.35; a new `gst_amount` column carries the 9% explicitly. Raw data mixes
   both bases.
3. **`PURCHASE` gets a surrogate `purchase_id`** — raw data has a duplicate `invoice_no`
   (`INV-042509-752`) so `invoice_no` can no longer be a key; it's demoted to a plain
   attribute.
4. **`INVOICE_SCAN` table added** — one row per physical paper invoice (69), holding the
   GST-inclusive `paper_total`. This is the reconciliation source against `PURCHASE`.
5. **`gst_reg_no` single canonical value per supplier** — raw invoices show conflicting
   reg numbers for the same supplier.
6. **`DAILY_SUMMARY` unified** — the 8 heterogeneous outlet CSVs (4 different header
   styles, 2 date formats) are normalised into one table with nullable `weather` /
   `wastage_kg`.
