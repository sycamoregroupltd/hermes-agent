#!/usr/bin/env bash
#==============================================================================
# budget-exhausted-actuator.sh  (STAGED / REVIEW-GATED devops cron entrypoint)
#
# Companion cron entrypoint for Hermes kanban task t_5b07664c.
#
# It runs the GATED iteration-budget-exhaustion recovery actuator
# (/home/frank/.hermes/scripts/kanban_budget_exhausted_actuator.sh) across the
# three production boards on a fixed cadence, reusing the actuator verbatim.
#
# SAFETY (this is the operator/Frank approval boundary):
#   * By design the actuator is DRY-RUN unless LIVE=true is passed in the
#     environment. This wrapper leaves LIVE UNSET, so even if the owning cron
#     job is enabled before approval, it ONLY classifies + logs the plan and
#     MUTATES NOTHING.
#   * To actually arm live auto-recover/escalate, an operator must edit the
#     cron invocation to `LIVE=true exec ...` (see staging runbook), or export
#     LIVE=true into the job environment. That is a separate approval gate from
#     merely enabling the cron.
#
# Explicit board paths are set here (not relied upon as implicit defaults) so
# the exact scope is reviewable and auditable. HERMES_HOME is pinned so the
# kanban_db module resolution is deterministic under cron.
#
# The actuator's own gates are preserved verbatim:
#   - no provider/model/fallback routing change
#   - no credentials
#   - no prod deploy
#   - no live/paper-trading mutation
#
# Logs: /home/frank/logs/budget-exhausted-actuator.log (rotated to last 2000 lines)
# Rollback: disable the cron entry (enabled=false) and/or leave LIVE unset; the
#           actuator mutates nothing in dry-run.
#==============================================================================
set -u

# Explicit, reviewable board scope for this staging task.
export BOARD_DIR="/home/frank/.hermes/kanban/boards"
export BOARDS="jarvis-os sycode-trading upero"

# Deterministic module resolution for the recovery tool's kanban_db import.
export HERMES_HOME="${HERMES_HOME:-/home/frank/.hermes/profiles/jarvis}"

# Pass through LIVE only if an operator explicitly set it when arming.
# Default (unset) => DRY-RUN (classify + log, no mutation).
exec /home/frank/.hermes/scripts/kanban_budget_exhausted_actuator.sh
