#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# cron_health_canary_wrapper.sh (t_634a8026)
#
# Profile-local jobs resolve `script` against the PROFILE-LOCAL scripts dir
# (profiles/jarvis/scripts/). The profile-local entry for this name is an exec
# shim that redirects here, to the canonical wrapper.
#
# Purpose: run the cron-health canary, route its output to the kanban router
# (cron_health_kanban_router.py), and re-emit the canary output unchanged so the
# job's existing delivery (discord:#fleet-reports) keeps working.
#
#   - canary stdout empty  -> healthy: router resolves any lingering cards
#   - canary stdout non-empty -> UNHEALTHY: router routes the alert block
# The router NEVER blocks delivery: its output goes to the router log; the
# canary's own stdout is always re-emitted (or silence preserved) for the job.

set -uo pipefail

CANARY="/home/frank/.hermes/profiles/jarvis/scripts/dgx_cron_health_canary.py"
ROUTER="/home/frank/.hermes/scripts/cron_health_kanban_router.py"
LOG="/home/frank/.hermes/state/cron-health-router-wrapper.log"
mkdir -p "$(dirname "$LOG")"

OUT="$( python3 "$CANARY" 2>&1 )"
rc=$?

if [ -z "$OUT" ]; then
    # Healthy tick: ask the router to resolve any lingering open cards.
    CRON_HEALTH_HEALTHY=1 "$ROUTER" <<< "" >>"$LOG" 2>&1 \
        || echo "CRON_HEALTH_ROUTER_FAILED rc=$? on healthy resolve $(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG"
    exit "$rc"
fi

# UNHEALTHY: route the alert block to the board, then re-emit for delivery.
# The wrapper routes non-empty stdout to cron_health_kanban_router.py
# regardless of canary rc. Do not treat canary sys.exit(1) as named-consumer
# proof for fe49f09f4e53 (t_ed054723): that dumps every existing canary ERROR
# into guard-bundle-tick-15m. The named-job ERROR is reserved in canary
# output and keyed separately by the router.
CRON_HEALTH_HEALTHY=0 "$ROUTER" <<< "$OUT" >>"$LOG" 2>&1 \
    || echo "CRON_HEALTH_ROUTER_FAILED rc=$? on unhealthy route $(date -u +%Y-%m-%dT%H:%M:%SZ)" >>"$LOG"
printf '%s\n' "$OUT"
exit "$rc"
