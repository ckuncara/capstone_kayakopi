# data/

Client data goes here. **This folder is git-ignored — never commit data or keys.**

## Expected contents (unzip the team zip from NTULearn here)

```
data/
├── sql/          # MySQL dump(s) — load with:
│                 #   docker compose exec -T mysql mysql -uroot -pcapstone < data/sql/<dump>.sql
├── outlets/      # 8 outlet CSV files
├── invoices/     # 70 scanned invoice images (naming TBD from the brief)
└── docs/         # WhatsApp / email exports
```

## Loading MySQL

```bash
docker compose up -d
docker compose exec -T mysql mysql -uroot -pcapstone < data/sql/<dump>.sql
docker compose exec mysql mysql -uroot -pcapstone -e 'SHOW DATABASES;'
```

Root password is `capstone` (see `docker-compose.yml`).
