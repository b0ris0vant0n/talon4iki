#!/usr/bin/env bash

set -euo pipefail

echo "[1/3] Building image and running tests..."
docker compose run --rm tests

echo "[2/3] Recreating bot container..."
docker compose down
docker compose --env-file .env.server up -d --build

echo "[3/3] Recent logs:"
docker compose logs --tail=50
