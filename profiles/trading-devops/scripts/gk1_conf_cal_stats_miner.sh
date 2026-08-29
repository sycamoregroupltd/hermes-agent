#!/usr/bin/env bash
# G-K1 recurring confidence_calibration_stats miner.
# Runs mine-confidence-calibration with --persist-stats against the host-local
# Postgres (the script's .env DATABASE_URL points at the Docker-internal
# 'supabase-db' host, unresolvable from the host; override to 127.0.0.1).
# Writes confidence_calibration_stats (paper/devops stats read-model).
set -euo pipefail
cd /home/frank/sycode-trading/server
export DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5432/postgres"
# Rolling ~48h window; only realized (non-synthetic) journeys via default.
bun scripts/mine-confidence-calibration.ts \
  --start "$(date -u -d '2 days ago' +%Y-%m-%d)" \
  --end "$(date -u +%Y-%m-%d)" \
  --min-samples 5 \
  --persist-stats \
  --output-dir /tmp/confcal-run 2>&1 | tail -5
