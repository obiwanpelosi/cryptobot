#!/usr/bin/env bash
# Ship a new version on the server: pull the code, rebuild, restart. Data and .env are untouched.
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build
docker image prune -f >/dev/null
sleep 15
docker compose ps
docker compose logs --tail 20 bot
