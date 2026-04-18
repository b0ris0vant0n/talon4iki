#!/usr/bin/env bash

set -euo pipefail

ENV_FILE=".env.server"
if [[ ! -f "$ENV_FILE" ]]; then
  ENV_FILE=".env"
fi
export BOT_ENV_FILE="$ENV_FILE"

echo "[1/3] Building image and running tests..."
docker compose --profile test run --rm tests

echo "[2/3] Recreating bot container..."
docker compose down
docker compose up -d --build

echo "[3/3] Recent logs:"
docker compose logs --tail=50
