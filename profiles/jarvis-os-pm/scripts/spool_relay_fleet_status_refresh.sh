#!/usr/bin/env bash
# t_1b0f7543 spool-relay wrapper for FleetStatusRefresh.
# Runs the original monitor (fleet-status-refresh.sh); if it emits non-empty stdout, writes an
# Alertmanager-webhook alert into the jarvis spool drain so exactly ONE profile
# (jarvis) holds the Discord token. Prints NOTHING (cron deliver=local).
# Interpreter mirrors the scheduler: .sh/.bash via bash, everything else python3.
set -uo pipefail
ORIGINAL="/home/frank/.hermes/profiles/jarvis-os-pm/scripts/fleet-status-refresh.sh"
SPOOL_DIR="/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool-fleet/incoming"
suffix="${ORIGINAL##*.}"
if [ "$suffix" = "sh" ] || [ "$suffix" = "bash" ]; then
  OUT="$("$ORIGINAL" 2>/dev/null)"
else
  OUT="$(python3 "$ORIGINAL" 2>/dev/null)"
fi
RC=$?
if [ -n "$OUT" ]; then
  python3 /home/frank/.hermes/scripts/spool_alert_write.py \
    --spool "$SPOOL_DIR" --alertname FleetStatusRefresh --severity warning \
    --summary "$OUT" >/dev/null 2>&1 || true
fi
exit $RC
