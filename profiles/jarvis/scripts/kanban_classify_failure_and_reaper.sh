#!/usr/bin/env bash
# Extends the kanban-classify-failure-cron (fe49f09f4e53) hygiene tick.
# Runs the existing diagnostics script, THEN the standing dead-PID reaper +
# needs_input digest consumer (kanban t_51d5a38b).
#
# The reaper is wired with the DRAIN-GATE ON: while an open 'DRAIN: jarvis-os'
# card (t_573abdb9) still exists, the reaper defers requeuing (backlog drains
# first so the weekly count baseline starts clean) but still emits/refreshes
# the ONE needs_input digest note. Reaping auto-activates once the drain closes.
#
# CANONICAL-COPY RULE: the reaper implementation lives in the canonical script
# /home/frank/.hermes/scripts/dead_pid_blocked_reaper.py. This shim only wires
# the cron to run it (Hermes cron resolves --script under the profile scripts/).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Existing diagnostics (unchanged behaviour).
"${SCRIPT_DIR}/kanban_classify_failure_recent.py"
diag_rc=$?

# 2. Standing reaper + digest consumer. Apply mode (this is the standing
#    consumer); drain-gate defers reaping until t_573abdb9 completes.
KANBAN_DEAD_PID_DRAIN_GATE=1 \
  env -u HERMES_KANBAN_DB -u HERMES_KANBAN_BOARD \
  python3 /home/frank/.hermes/scripts/dead_pid_blocked_reaper.py --apply
reaper_rc=$?

# Return nonzero only if BOTH stages failed (a clean no-op returns 0).
if [[ $diag_rc -ne 0 && $reaper_rc -ne 0 ]]; then
  echo "kanban_classify_failure_and_reaper: diag rc=$diag_rc reaper rc=$reaper_rc" >&2
  exit 1
fi
exit 0
