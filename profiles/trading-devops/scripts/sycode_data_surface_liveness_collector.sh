#!/usr/bin/env bash
# Cron shim for the data-surface liveness collector (t_580ec47d).
#
# The Hermes cron runner resolves `script` under <profile>/scripts/ and REJECTS
# symlinks and out-of-dir paths, so this is a real in-directory wrapper that execs
# the canonical script in the sycode-trading repo (single source of truth).
#
# Emits monitoring/node-exporter-textfile/sycode_data_surface_liveness.prom, which the
# node-exporter textfile collector exports and Prometheus scrapes.
# Read-only against Postgres (count(*)/max(ts)) and read-only against Prometheus /api/v1.
set -uo pipefail

CANONICAL="/home/frank/sycode-trading/monitoring/scripts/sycode_data_surface_liveness_collector.py"

# The canonical file may live in a worktree while this task is in review; fall back to
# the branch worktree only if the main checkout does not have it yet.
if [ ! -f "$CANONICAL" ]; then
  ALT="/home/frank/sycode-trading/.worktrees/t_580ec47d/monitoring/scripts/sycode_data_surface_liveness_collector.py"
  if [ -f "$ALT" ]; then
    CANONICAL="$ALT"
  else
    echo "sycode-data-surface: collector not found (looked in main checkout and t_580ec47d worktree)" >&2
    exit 1
  fi
fi

exec python3 "$CANONICAL"