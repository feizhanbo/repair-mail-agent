#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${SERVER_PROJECT_DIR:-/opt/repair-mail-agent}"

cd "$PROJECT_DIR"

echo "Pulling latest code..."
git pull --ff-only origin main

echo "Building and restarting containers..."
docker compose up -d --build

echo "Current service status:"
docker compose ps

