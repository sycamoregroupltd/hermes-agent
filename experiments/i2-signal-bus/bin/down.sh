#!/usr/bin/env bash
# Tear down the ephemeral paper-broker Redis (and its data volume).
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose -f docker-compose.yml down -v
