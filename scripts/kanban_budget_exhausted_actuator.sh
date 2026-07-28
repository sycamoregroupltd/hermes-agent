#!/usr/bin/env bash
#==============================================================================
# kanban_budget_exhausted_actuator.sh
#
# Periodic actuator that closes the iteration-budget kill recurrence gap.
#
# The recovery tool (/home/frank/.hermes/scripts/kanban_budget_exhausted_recovery.py)
# already implements the GATED mode required by t_afd36632:
#   * read-only classify each tick;
#   * auto-apply (kb.unblock_task) only the AUTO-RECOVER subset
#     (budget-kill error prefix AND NOT embedded_error AND cf<=1);
#     this clears last_failure_error + resets consecutive_failures so the next
#     dispatch gets a fresh budget and is re-queued.
#   * ESCALATE (kb.block_task kind='needs_input' + verdict comment naming the
#     reviewer) the cf>1 / embedded subset — never auto-retried.
#
# This wrapper simply runs that tool across all three production boards on a
# fixed cadence. It does NOT reimplement the classifier; it reuses the actuator.
#
# GATING (acceptance #3 — operator approval before any auto-apply goes live):
#   By default the runner is DRY-RUN. It classifies every board, logs the plan,
#   and MUTATES NOTHING. To actually auto-apply the bounded recoveries and write
#   the escalation verdicts, an operator must explicitly set LIVE=true:
#
#       LIVE=true /home/frank/.hermes/scripts/kanban_budget_exhausted_actuator.sh
#
#   This is the Frank/A3 operator gate: the cron entry installed by the
#   integration-builder leaves LIVE unset (dry-run) until that approval lands.
#
# No provider/model/fallback routing, no creds, no prod deploy, paper-trading
# untouched — the recovery script's own gates are preserved verbatim.
#==============================================================================
set -u

RECOVERY_PY="/home/frank/.hermes/scripts/kanban_budget_exhausted_recovery.py"
BOARD_DIR="${BOARD_DIR:-/home/frank/.hermes/kanban/boards}"
BOARDS="${BOARDS:-jarvis-os sycode-trading upero}"
LOG_DIR="/home/frank/logs"
LOG_FILE="${LOG_DIR}/budget-exhausted-actuator.log"

mkdir -p "$LOG_DIR"
ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Rotate the log (keep last 2000 lines) so the periodic cron can't grow it
# unbounded. Non-destructive; runs only when the log already exists.
if [[ -f "$LOG_FILE" ]]; then
  tail -n 2000 "$LOG_FILE" > "${LOG_FILE}.tmp" 2>/dev/null && mv "${LOG_FILE}.tmp" "$LOG_FILE"
fi

# LIVE defaults to off. Only the literal string "true" (case-insensitive) arms
# auto-apply. Anything else (including unset) is a safe dry-run.
LIVE_VAL="${LIVE:-false}"
if [[ "${LIVE_VAL,,}" == "true" ]]; then
  MODE="LIVE"
  APPLY_FLAG="--apply"
else
  MODE="DRY-RUN"
  APPLY_FLAG=""
fi

echo "[$ts] [$MODE] budget-exhausted actuator start" >> "$LOG_FILE"

overall=0
for board in $BOARDS; do
  # Each board invocation logs its own bucket counts to the same log file.
  if out=$(python3 "$RECOVERY_PY" --board-dir "$BOARD_DIR" --board "$board" $APPLY_FLAG 2>&1); then
    echo "$out" | sed "s/^/[$board] /" >> "$LOG_FILE"
  else
    rc=$?
    echo "[$board] ERROR rc=$rc" >> "$LOG_FILE"
    echo "$out" | sed "s/^/[$board][ERR] /" >> "$LOG_FILE"
    overall=$rc
  fi
done

# Emit a one-line heartbeat to stderr too, so cron mail/capture sees the mode.
echo "[$ts] [$MODE] actuator done (exit $overall)" >> "$LOG_FILE"
exit $overall
