#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
./scripts/check_environment.sh

set -a
# shellcheck disable=SC1091
source .env
set +a
KAYA_DB_READONLY_USER="${KAYA_DB_READONLY_USER:-copilot_reader}"
KAYA_DB_READONLY_PASSWORD="${KAYA_DB_READONLY_PASSWORD:-change_me_readonly}"
export KAYA_DB_READONLY_USER KAYA_DB_READONLY_PASSWORD

identifier_pattern='^[A-Za-z0-9_]+$'
credential_pattern='^[A-Za-z0-9_.@%+-]+$'
[[ "$KAYA_DB_READONLY_USER" =~ $identifier_pattern ]] || {
  echo "error: unsafe KAYA_DB_READONLY_USER" >&2
  exit 2
}
[[ "$KAYA_DB_READONLY_PASSWORD" =~ $credential_pattern ]] || {
  echo "error: unsafe KAYA_DB_READONLY_PASSWORD" >&2
  exit 2
}
[[ "$KAYA_DB_READONLY_USER" != "$KAYA_DB_USER" ]] || {
  echo "error: Copilot read-only account must differ from the ingestion database account" >&2
  exit 2
}

mysql_command=(docker compose exec -T -e "MYSQL_PWD=$KAYA_DB_ROOT_PASSWORD" mysql mysql -uroot)
"${mysql_command[@]}" --execute="CREATE USER IF NOT EXISTS '$KAYA_DB_READONLY_USER'@'%' IDENTIFIED BY '$KAYA_DB_READONLY_PASSWORD'; ALTER USER '$KAYA_DB_READONLY_USER'@'%' IDENTIFIED BY '$KAYA_DB_READONLY_PASSWORD'; REVOKE ALL PRIVILEGES, GRANT OPTION FROM '$KAYA_DB_READONLY_USER'@'%'; GRANT SELECT ON \`kayakopi\`.* TO '$KAYA_DB_READONLY_USER'@'%'; FLUSH PRIVILEGES;"
echo "Provisioned MySQL SELECT-only account '$KAYA_DB_READONLY_USER' for the Copilot."
