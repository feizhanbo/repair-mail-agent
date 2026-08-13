#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${SERVER_PROJECT_DIR:-/opt/repair-mail-agent}"

cd "$PROJECT_DIR"

echo "Pulling latest code..."
git pull --ff-only origin main
DEPLOY_COMMIT="$(git rev-parse HEAD)"
export APP_RELEASE_COMMIT="$DEPLOY_COMMIT"

echo "Building containers..."
docker compose build

# Parse only the literal setting; never source .env as shell code.
WORKFLOW_ENGINE_VALUE="$(awk -F= '
  /^[[:space:]]*WORKFLOW_ENGINE[[:space:]]*=/ {
    value=$0
    sub(/^[^=]*=/, "", value)
    gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
    gsub(/^"|"$/, "", value)
    print tolower(value)
  }
' .env | tail -n 1)"
WORKFLOW_ENGINE_VALUE="${WORKFLOW_ENGINE_VALUE:-legacy}"

echo "Starting MySQL business database..."
docker compose up -d mysql

if [[ "$WORKFLOW_ENGINE_VALUE" == "langgraph" ]]; then
  LANGGRAPH_RELEASE_EVIDENCE_VALUE="$(awk -F= '
    /^[[:space:]]*LANGGRAPH_RELEASE_EVIDENCE_FILE[[:space:]]*=/ {
      value=$0
      sub(/^[^=]*=/, "", value)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      gsub(/^"|"$/, "", value)
      print value
    }
  ' .env | tail -n 1)"
  if [[ -z "$LANGGRAPH_RELEASE_EVIDENCE_VALUE" ]]; then
    echo "LANGGRAPH_RELEASE_EVIDENCE_FILE is required for langgraph deployment" >&2
    exit 1
  fi
  case "$LANGGRAPH_RELEASE_EVIDENCE_VALUE" in
    /app/release-evidence/*) ;;
    *)
      echo "LANGGRAPH_RELEASE_EVIDENCE_FILE must be under /app/release-evidence/" >&2
      exit 1
      ;;
  esac
  echo "Verifying pre-production three-probe release evidence..."
  docker compose --profile langgraph run --rm backend-api python -m tools.audit_langgraph_release \
    --verify-local-release-evidence "$LANGGRAPH_RELEASE_EVIDENCE_VALUE" \
    --expected-commit "$DEPLOY_COMMIT" \
    --evidence-root /app/release-evidence \
    --max-evidence-age-hours 168
  echo "Starting dedicated LangGraph checkpoint database..."
  docker compose --profile langgraph up -d langgraph-postgres
  echo "Initializing dedicated LangGraph checkpoint schema..."
  docker compose --profile langgraph run --rm backend-api python -m tools.setup_langgraph_checkpoint
  echo "Auditing LangGraph release configuration..."
  docker compose --profile langgraph run --rm backend-api python -m tools.audit_langgraph_release
fi

echo "Starting application services..."
docker compose up -d

echo "Current service status:"
docker compose ps

