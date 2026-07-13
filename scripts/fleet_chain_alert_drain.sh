#!/usr/bin/env bash
# fleet_chain_alert_drain.sh — every-5-minute drain that reads the fleet-chain
# validator's sidecar output and routes DEAD-chain alerts to #critical-alerts.
#
# The canonical validator writes its sidecar to:
#   ~/.hermes/profiles/jarvis/cron/output/fleet_chain_alert.txt
# This shim reads that file. If non-empty (exit code 1 from validator = DEAD
# rungs), cat the content for cron delivery to #critical-alerts.
# If empty or absent, silent exit — no noise when the fleet is healthy.
set -uo pipefail

SIDECAR="/home/frank/.hermes/profiles/jarvis/cron/output/fleet_chain_alert.txt"

if [ -s "$SIDECAR" ]; then
  cat "$SIDECAR"
else
  # Silent when green — nothing to report
  exit 0
fi
