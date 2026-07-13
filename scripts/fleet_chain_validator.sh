#!/usr/bin/env bash
# fleet_chain_validator.sh — cron-facing exec shim for the canonical Python validator.
# CANONICAL SOURCE: ~/.hermes/scripts/fleet_chain_validator.py
# This shim exec()s the canonical script so cron can capture its stdout.
# The Python script internally writes its alert sidecar to
#   ~/.hermes/profiles/jarvis/cron/output/fleet_chain_alert.txt
# per CRON_OUTPUT_DIR in the canonical source.
set -uo pipefail

CANONICAL="/home/frank/.hermes/scripts/fleet_chain_validator.py"

if [ ! -x "$CANONICAL" ]; then
  echo "FATAL: canonical validator not found or not executable: $CANONICAL"
  exit 2
fi

exec "$CANONICAL" "$@"
