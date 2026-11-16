# data/

Client data lives here. **Everything in this folder is git-ignored except the two
markdown deliverables** (`README.md`, `DATA_AUDIT.md`) — client data and keys never
enter the repo.

## Actual package contents (team1_kaya_kopi_package.zip, 16 Nov 2026)

```
data/
├── CLIENT_BRIEF.md                # client's brief (read first)
├── BRIEFING_team1_kaya_kopi.pdf   # same briefing, PDF version
├── sql/kayakopi_pos.sql           # MySQL dump: outlets, menu_items,
│                                  #   pos_transactions (33,059),
│                                  #   suppliers, supplier_purchases (178)
├── tabular/daily_summary_*.csv    # 8 outlet files (2025-07-01 → 2026-06-30)
│                                  #   NOTE: TPY-02 & YIS-02 are MM/DD/YYYY,
│                                  #   the other six are DD/MM/YYYY
├── images/supplier_invoices/      # 69 scanned invoice JPEGs (brief says 70!)
├── docs/emails/                   # 2026-05-18 outlet rename, 2026-06-02 finance
└── docs/whatsapp_exports/         # ops chat export
```

## Loading the MySQL dump

The dump is plain SQL — load into any MySQL 8:

```bash
mysql -uroot -p < data/sql/kayakopi_pos.sql
mysql -uroot -e "USE kayakopi; SHOW TABLES; SELECT COUNT(*) FROM pos_transactions;"
```

No Docker needed if MySQL runs locally (e.g. in WSL); the dump has no
server-specific settings.

## Known data issues

See `data/DATA_AUDIT.md` and `docs/data_quality_traps.md` — 11 traps logged,
including the AMK-03/AMK-3A rename, mixed GST basis, duplicate invoice number,
and the two US-date-format CSVs.
