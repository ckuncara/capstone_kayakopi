#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

data_dir=data/local_chroma_db
database_file="$data_dir/chroma.sqlite3"
mkdir -p "$data_dir"

host_uid="$(id -u)"
host_gid="$(id -g)"
export KAYA_DOCKER_UID="${KAYA_DOCKER_UID:-$host_uid}"
export KAYA_DOCKER_GID="${KAYA_DOCKER_GID:-$host_gid}"

if [[ "$KAYA_DOCKER_UID:$KAYA_DOCKER_GID" == "$host_uid:$host_gid" ]]; then
  if [[ -w "$data_dir" && -x "$data_dir" ]] &&
    [[ ! -e "$database_file" || -w "$database_file" ]]; then
    exit 0
  fi
else
  if docker compose run --rm --no-deps --entrypoint /bin/sh chroma -c \
    'test -w /data && test -x /data && { test ! -e /data/chroma.sqlite3 || test -w /data/chroma.sqlite3; }'; then
    exit 0
  fi
fi

echo "error: Chroma needs write access to $data_dir and its SQLite file as UID:GID $KAYA_DOCKER_UID:$KAYA_DOCKER_GID." >&2
echo "Check the directory and SQLite file ownership before starting Chroma." >&2
exit 2
