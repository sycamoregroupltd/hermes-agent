#!/usr/bin/env bash
# Cron entrypoint for the kanban diagnostics actuator (gap card t_9e4789df).
#
# UNARMED: runs in DRY-RUN. It plans and logs what it would do but writes
# nothing to any board. To arm it after review, change DRY_RUN=1 to DRY_RUN=0
# below (which adds --apply --allow-card).
#
# Silent when there is nothing to plan (watchdog convention).
set -uo pipefail

DRY_RUN=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTUATOR="${SCRIPT_DIR}/kanban_diagnostics_actuator.py"

if [[ ! -f "$ACTUATOR" ]]; then
  echo "KANBAN_DIAGNOSTICS_ACTUATOR ERROR: missing $ACTUATOR" >&2
  exit 1
fi

ARGS=(--boards jarvis-os sycode-trading)
if [[ "$DRY_RUN" -eq 0 ]]; then
  ARGS+=(--apply --allow-card)
fi

# HERMES_KANBAN_DB/BOARD in the ambient env override --board; strip them.
env -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD \
  python3 "$ACTUATOR" "${ARGS[@]}"
rc=$?

if [[ $rc -ne 0 ]]; then
  echo "KANBAN_DIAGNOSTICS_ACTUATOR exited rc=$rc" >&2
fi
exit $rc
