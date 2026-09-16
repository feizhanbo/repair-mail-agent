#!/usr/bin/env bash
set -euo pipefail

WARNING_PERCENT="${DISK_WARNING_PERCENT:-80}"
CRITICAL_PERCENT="${DISK_CRITICAL_PERCENT:-90}"
PROJECT_DIR="${SERVER_PROJECT_DIR:-/opt/repair-mail-agent}"

paths=("/" "$PROJECT_DIR/logs")
if command -v docker >/dev/null 2>&1; then
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  if [ -n "$docker_root" ]; then paths+=("$docker_root"); fi
  mysql_mount="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/mysql"}}{{.Source}}{{end}}{{end}}' repair-mysql 2>/dev/null || true)"
  if [ -n "$mysql_mount" ]; then paths+=("$mysql_mount"); fi
fi

status=0
seen=""
for path in "${paths[@]}"; do
  [ -e "$path" ] || continue
  filesystem="$(df -P "$path" | awk 'NR==2 {for (i=1; i<=NF-5; i++) printf "%s%s", (i>1 ? " " : ""), $i; print ""}')"
  case " $seen " in *" $filesystem "*) continue ;; esac
  seen="$seen $filesystem"
  used="$(df -P "$path" | awk 'NR==2 {gsub(/%/, "", $(NF-1)); print $(NF-1)}')"
  level="ok"
  code=0
  if [ "$used" -ge "$CRITICAL_PERCENT" ]; then
    level="critical"
    code=2
  elif [ "$used" -ge "$WARNING_PERCENT" ]; then
    level="warning"
    code=1
  fi
  printf '{"event":"disk_usage_check","path":"%s","filesystem":"%s","used_percent":%s,"level":"%s"}\n' \
    "$path" "$filesystem" "$used" "$level"
  if [ "$code" -gt "$status" ]; then status="$code"; fi
done

exit "$status"
