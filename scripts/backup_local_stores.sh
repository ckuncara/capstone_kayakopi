#!/usr/bin/env bash
set -euo pipefail

STAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="./backups/${STAMP}"
mkdir -p "${BACKUP_DIR}"

if [ -f "./data/kaya_ops.db" ]; then
  sqlite3 ./data/kaya_ops.db ".backup '${BACKUP_DIR}/kaya_ops.db'"
fi

if [ -d "./data/local_chroma_db" ]; then
  cp -r ./data/local_chroma_db "${BACKUP_DIR}/local_chroma_db"
fi

find ./backups -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} +
echo "Snapshot saved to ${BACKUP_DIR}"