#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

# Basic environment validation only. No --require-data: ingestion is a later step.
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

# OCR runs through the OpenAI API, so no host runtime/ocr or runtime/structured
# directories are needed. Chroma and upload records still use data/.
mkdir -p data/local_chroma_db data/upload_assistant

# Build only the images needed for the upload assistant and later ingestion.
# The "test" profile is intentionally excluded: docker/tests.Dockerfile copies
# tests/, which .dockerignore excludes, so that image cannot be built here.
docker compose --profile setup build

echo "Upload Assistant images are ready."
echo "Next: run the data ingestion scripts, then ./scripts/start_upload_assistant.sh"
