#!/usr/bin/env bash
# CANONICAL-COPY RULE — exec shim only. Edit the canonical file at
# /home/frank/.hermes/scripts/jarvis_os_pm_nous_expiry_preflight.py, not this copy.
# Invoker: jarvis-os-pm profile cron job `jarvis-os-pm-nous-expiry-preflight`.
set -uo pipefail
ACTUATOR="/home/frank/.hermes/scripts/jarvis_os_pm_nous_expiry_preflight.py"
if [ ! -f "$ACTUATOR" ]; then
  echo 'jarvis-os-pm-nous-expiry-preflight: canonical actuator missing — cannot run' >&2
  exit 1
fi
exec python3 "$ACTUATOR" "$@"
