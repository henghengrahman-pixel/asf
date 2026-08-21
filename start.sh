#!/usr/bin/env bash
set -euo pipefail
export PORT="${PORT:-8080}"
export DATA_DIR="${DATA_DIR:-/data}"
export DB_PATH="${DB_PATH:-$DATA_DIR/marketing8008.db}"
export DP_INTERNAL_PORT="${DP_INTERNAL_PORT:-3001}"
export DP_INTERNAL_TOKEN="${DP_INTERNAL_TOKEN:-crm-dp-internal-2026}"
mkdir -p "$DATA_DIR" "$DATA_DIR/dp_checker"
(
  cd /app/dp-service
  PORT="$DP_INTERNAL_PORT" DATA_DIR="$DATA_DIR/dp_checker" INTERNAL_TRUST_TOKEN="$DP_INTERNAL_TOKEN" SESSION_SECRET="${SESSION_SECRET:-${SECRET_KEY:-marketing8008-dp-session-2026}}" node src/server.js
) &
DP_PID=$!
cleanup(){ kill "$DP_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${DP_INTERNAL_PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
exec gunicorn --workers 1 --threads 8 --timeout 180 --bind "0.0.0.0:$PORT" app:app
