#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-restart}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_DIR="${SERVER_PROJECT_DIR:-$SCRIPT_DIR}"
case "$MODE" in
  restart|recreate) ;;
  *) echo "usage: $0 [restart|recreate]" >&2; exit 64 ;;
esac

cd "$PROJECT_DIR"
current_http_port="$(docker port repair-nginx 80/tcp 2>/dev/null | awk -F: 'NR==1 {print $NF}' || true)"
HTTP_PORT="${NGINX_HTTP_PORT:-${current_http_port:-80}}"
request_id="req_verify_$(date -u +%Y%m%d%H%M%S)_$$"
correlation_id="corr_verify_$(date -u +%Y%m%d%H%M%S)_$$"
headers_file="$(mktemp)"
body_file="$(mktemp)"
nginx_logs_file="$(mktemp)"
backend_logs_file="$(mktemp)"
trap 'rm -f "$headers_file" "$body_file" "$nginx_logs_file" "$backend_logs_file"' EXIT

docker compose config >/dev/null
docker compose exec -T nginx nginx -t
mysql_mount_before="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/mysql"}}{{.Source}}{{end}}{{end}}' repair-mysql)"
test -n "$mysql_mount_before"

status="$(curl --silent --show-error --output "$body_file" --dump-header "$headers_file" \
  --write-out '%{http_code}' \
  -H "X-Request-ID: $request_id" \
  -H "X-Correlation-ID: $correlation_id" \
  "http://127.0.0.1:${HTTP_PORT}/api/v1/auth/me")"
test "$status" = "401"
actual_request_id="$(awk 'BEGIN{IGNORECASE=1} /^X-Request-ID:/ {gsub("\r", ""); sub(/^[^:]+:[[:space:]]*/, ""); print; exit}' "$headers_file")"
actual_correlation_id="$(awk 'BEGIN{IGNORECASE=1} /^X-Correlation-ID:/ {gsub("\r", ""); sub(/^[^:]+:[[:space:]]*/, ""); print; exit}' "$headers_file")"
test "$actual_request_id" = "$request_id"
test "$actual_correlation_id" = "$correlation_id"
grep -Fq "\"request_id\":\"$request_id\"" "$body_file"

for _ in $(seq 1 10); do
  if grep -R -Fq "\"request_id\":\"$request_id\"" logs/runtime/backend*.jsonl 2>/dev/null; then break; fi
  sleep 1
done
grep -R -Fq "\"request_id\":\"$request_id\"" logs/runtime/backend*.jsonl
docker compose logs nginx > "$nginx_logs_file"
grep -Fq "\"request_id\":\"$request_id\"" "$nginx_logs_file"

if [ "$MODE" = "recreate" ]; then
  docker compose up -d --force-recreate --no-deps backend-api
else
  docker compose restart backend-api
fi

for attempt in $(seq 1 30); do
  readiness_body="$(curl --fail --silent "http://127.0.0.1:${HTTP_PORT}/readiness" 2>/dev/null || true)"
  case "$readiness_body" in *'"status":"ready"'*) break ;; esac
  if [ "$attempt" -eq 30 ]; then
    docker compose logs --tail=200 backend-api nginx
    exit 1
  fi
  sleep 2
done

grep -R -Fq "\"request_id\":\"$request_id\"" logs/runtime/backend*.jsonl
if [ "$MODE" = "restart" ]; then
  docker compose logs backend-api > "$backend_logs_file"
  grep -Fq "\"request_id\":\"$request_id\"" "$backend_logs_file"
fi
mysql_mount_after="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/mysql"}}{{.Source}}{{end}}{{end}}' repair-mysql)"
test "$mysql_mount_after" = "$mysql_mount_before"

set +e
bash tools/check_disk_usage.sh
disk_status=$?
set -e
if [ "$disk_status" -ge 2 ]; then
  echo '{"event":"observability_acceptance_failed","reason":"disk_critical"}' >&2
  exit 2
fi

printf '{"event":"observability_acceptance_completed","mode":"%s","request_id":"%s","correlation_id":"%s"}\n' \
  "$MODE" "$request_id" "$correlation_id"
