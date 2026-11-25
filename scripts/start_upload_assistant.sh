#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

./scripts/check_environment.sh
if ! grep -Eq '^OPENAI_API_KEY=.+$' .env; then
  echo "error: OPENAI_API_KEY is empty or missing in .env" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

export KAYA_DOCKER_UID="${KAYA_DOCKER_UID:-$(id -u)}"
export KAYA_DOCKER_GID="${KAYA_DOCKER_GID:-$(id -g)}"
bash ./scripts/prepare_chroma_data.sh

docker compose up -d mysql
mysql_command=(docker compose exec -T -e "MYSQL_PWD=$KAYA_DB_ROOT_PASSWORD" mysql mysql -uroot)
loaded=0
for _ in $(seq 1 30); do
  if [[ "$("${mysql_command[@]}" --batch --skip-column-names --execute="SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='kayakopi' AND table_name='source_document';" 2>/dev/null || true)" == "1" ]]; then
    loaded=1
    break
  fi
  sleep 2
done
if [[ "$loaded" -ne 1 ]]; then
  echo "error: the application database is not loaded; run ./scripts/reload_data.sh first" >&2
  exit 2
fi

upload_port="${UPLOAD_AI_PORT:-8502}"
override="$(mktemp)"
trap 'rm -f "$override"' EXIT
cat > "$override" <<YAML
services:
  copilot:
    command: !override
      - python
      - -m
      - streamlit
      - run
      - src/kaya_kopi/upload_assistant/app.py
      - --server.address=0.0.0.0
      - --server.port=8502
      - --browser.gatherUsageStats=false
    ports: !override
      - "127.0.0.1:${upload_port}:8502"
YAML

mkdir -p data/upload_assistant
docker compose -f docker-compose.yml -f "$override" up -d --build --remove-orphans copilot

echo "Kaya Upload Desk is starting at http://localhost:${upload_port}"
echo "It uses the same copilot container, chat database and data volumes as streamlit_app.py."
echo "View logs with: docker compose logs -f copilot"
echo "Stop it with:   docker compose down"
