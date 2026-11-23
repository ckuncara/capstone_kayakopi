# data/

Download YOUR team's zip from NTULearn (released at the Mon 16 Nov briefing) and unzip its
contents into this folder, so you end up with `data/sql/`, `data/tabular/`, `data/images/`,
`data/docs/` (exact folders vary by company).

Everything in this folder except this file and `DATA_AUDIT.md` is **git-ignored on purpose**.
Client data never enters git — a data file in your commit history costs marks and cannot be
fully deleted.

## What is in our package (team1_kaya_kopi_package.zip, 16 Nov 2026)

```
data/
├── CLIENT_BRIEF.md                # client's brief (read first)
├── BRIEFING_team1_kaya_kopi.pdf   # same briefing, PDF version
├── sql/kayakopi_pos.sql           # MySQL dump: outlets, menu_items,
│                                  #   pos_transactions (33,059),
│                                  #   suppliers, supplier_purchases (178)
├── tabular/
│   ├── daily_summary_*.csv        # 8 outlet files (2025-07-01 -> 2026-06-30)
│   │                              #   NOTE: TPY-02 & YIS-02 are MM/DD/YYYY,
│   │                              #   the other six are DD/MM/YYYY
│   └── staff_roster_jun2026.xlsx  # CONFIDENTIAL — pay rates; team-only, never git
├── images/supplier_invoices/      # 69 scanned invoice JPEGs (brief says 70!)
└── docs/
    ├── emails/                    # 2026-05-18 outlet rename, 2026-06-02 finance
    └── whatsapp_exports/          # ops chat export
```

## Loading the MySQL dump

The dump is plain SQL — load into any MySQL 8:

```bash
mysql -uroot -p < data/sql/kayakopi_pos.sql
mysql -uroot -e "USE kayakopi; SHOW TABLES; SELECT COUNT(*) FROM pos_transactions;"
```

No Docker needed if MySQL runs locally (e.g. in WSL); the dump has no server-specific
settings.

## Known data issues

See `data/DATA_AUDIT.md` and `docs/data_quality_traps.md` — traps logged, including the
AMK-03/AMK-3A rename, mixed GST basis, duplicate invoice number, and the two US-date-format
CSVs.

## Packaged startup workflow

For the packaged startup workflow, confirm these paths exist after copying the package:

```text
data/sql/kayakopi_pos.sql
data/tabular/staff_roster_jun2026.xlsx
data/images/supplier_invoices/
data/docs/
```

Do not copy generated `runtime/` files from another machine. `scripts/reload_data.sh`
regenerates OCR sidecars and normalized staging files from the private source package.
