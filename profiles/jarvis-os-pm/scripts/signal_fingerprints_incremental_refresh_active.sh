#!/usr/bin/env bash
set -euo pipefail

# Active-scheduler wrapper for jarvis-os/t_f663c196.
# Delegates to the repaired db-architect script without enabling the dormant
# db-architect gateway/ticker. The target script is paper/data-only and prints
# only when rows are inserted; empty stdout keeps no-op cron ticks silent.
#
# Cadence tuning (2026-08-07):
# - LOOKBACK_HOURS=1: only scan journeys from the last 1 hour, so the cron
#   processes near-real-time rows within ~15 min. A 24h lookback caused the
#   batch buffer to always carry stale backlog, pushing p95 lag to ~22h.
# - BATCH_SIZE=5000: handle burst volume (~5k journeys/hr) in a single tick.
# - MAX_BATCHES=5: cap at 25k rows per run; if backlog exists it clears in
#   one cycle without starving new rows.
export SIGNAL_FINGERPRINT_LOOKBACK_HOURS="${SIGNAL_FINGERPRINT_LOOKBACK_HOURS:-1}"
export SIGNAL_FINGERPRINT_BATCH_SIZE="${SIGNAL_FINGERPRINT_BATCH_SIZE:-5000}"
export SIGNAL_FINGERPRINT_MAX_BATCHES="${SIGNAL_FINGERPRINT_MAX_BATCHES:-5}"
exec /home/frank/.hermes/profiles/db-architect/scripts/signal_fingerprints_incremental_refresh.py
