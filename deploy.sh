#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SERVER_PROJECT_DIR:-$SCRIPT_DIR}"

cd "$PROJECT_DIR"

if [ ! -f ".env" ]; then
  echo "ERROR: .env not found in $PROJECT_DIR. Create it from .env.example and fill real values before deploying." >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "ERROR: deployment stopped because the server worktree has uncommitted changes." >&2
  git status --short >&2
  exit 1
fi

current_http_port="$(docker port repair-nginx 80/tcp 2>/dev/null | awk -F: 'NR==1 {print $NF}' || true)"
export NGINX_HTTP_PORT="${NGINX_HTTP_PORT:-${current_http_port:-80}}"

echo "Pulling latest code..."
if ! git pull --ff-only origin main; then
  echo "ERROR: git pull failed. Verify the private origin and server credentials." >&2
  exit 1
fi

mkdir -p logs/runtime logs/ai
test -w logs/runtime
test -w logs/ai

echo "Building and restarting containers..."
docker compose up -d --build

echo "Waiting for application readiness..."
for attempt in $(seq 1 30); do
  readiness_body="$(curl --fail --silent "http://127.0.0.1:${NGINX_HTTP_PORT}/readiness" 2>/dev/null || true)"
  case "$readiness_body" in *'"status":"ready"'*) break ;; esac
  if [ "$attempt" -eq 30 ]; then
    docker compose logs --tail=200 backend-api nginx
    exit 1
  fi
  sleep 2
done

echo "Current service status:"
docker compose ps
