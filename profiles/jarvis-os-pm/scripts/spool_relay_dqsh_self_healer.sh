#!/usr/bin/env bash
# t_1b0f7543 spool-relay wrapper for DqshSelfHealerDaemon.
# Runs the original monitor (run_dqsh.sh); if it emits non-empty stdout, writes an
# Alertmanager-webhook alert into the jarvis spool drain so exactly ONE profile
# (jarvis) holds the Discord token. Prints NOTHING (cron deliver=local).
set -uo pipefail
ORIGINAL="/home/frank/.hermes/profiles/jarvis-os-pm/scripts/run_dqsh.sh"
SPOOL_DIR="/home/frank/.hermes/profiles/jarvis/state/alertmanager-spool/incoming"
OUT="$(bash "$ORIGINAL" 2>/dev/null)"
RC=$?
if [ -n "$OUT" ]; then
  python3 /home/frank/.hermes/scripts/spool_alert_write.py \
    --spool "$SPOOL_DIR" --alertname DqshSelfHealerDaemon --severity critical \
    --summary "$OUT" >/dev/null 2>&1 || true
fi
exit $RC
