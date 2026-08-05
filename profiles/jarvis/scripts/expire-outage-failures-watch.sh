#!/usr/bin/env bash
# Outage-failure expiry actuator — cron wrapper (jarvis-os/t_411e72b4).
#
# Periodic no-agent LOOP: expires consecutive_failures accrued inside
# fleet-wide health outage windows (per unified-health-probe verdict history)
# once health returns PASS. Re-queues breaker-blocked cards via the sanctioned
# kanban API so they are re-dispatchable WITHOUT a governor hand-resurrection.
#
# Contract (final stdout line is the wakeAgent gate consumed by the cron
# scheduler):
#   {"wakeAgent": true}  -> expiry/requeue was applied (report delivered)
#   {"wakeAgent": false} -> clean tick / nothing to do (silent)
# Fail-open: any script crash exits non-zero -> scheduler surfaces an error
# alert instead of silently wedging. Never expires during an active outage.
set -uo pipefail

PYTHON="$(command -v python3)"
ACTUATOR="/home/frank/.hermes/scripts/expire_outage_failures.py"

if [ ! -f "$ACTUATOR" ]; then
  echo 'expire-outage-failures: actuator script missing — cannot run' >&2
  echo '{"wakeAgent": true}'
  exit 1
fi

exec "$PYTHON" "$ACTUATOR" --apply \
  --health-log /home/frank/.hermes/profiles/jarvis/cron/output/unified_health_canary.jsonl
