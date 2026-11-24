#!/usr/bin/env bash
set -euo pipefail

: "${KAYA_DB_HOST:=mysql}"
: "${KAYA_DB_PORT:=3306}"
: "${KAYA_DB_USER:?KAYA_DB_USER is required}"
: "${KAYA_DB_PASSWORD:?KAYA_DB_PASSWORD is required}"
#python -m kaya_kopi.ingestion.parsers.invoice_ocr
#python -m kaya_kopi.ingestion.parsers.invoice
python -m kaya_kopi.ingestion.parsers.image_ingestion_openai
python -m kaya_kopi.ingestion.parsers.text_documents
python -m kaya_kopi.ingestion.parsers.tabular
python -m kaya_kopi.ingestion.parsers.staff_roster
python -m kaya_kopi.ingestion.loader.mysql
