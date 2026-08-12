#!/usr/bin/env bash
# Experiment-factory weekly cadence + LIVE-CRITICAL REFERENCE for the launchers.
# Dual purpose: (1) reports the factory pulse; (2) by naming the launcher scripts
# by absolute path below, it makes them "referenced by an enabled cron job" so the
# post-checkout self-heal guard (cron_untracked_script_guard.py) restores them after
# any worker branch-switch — closing the gap that wiped them 2026-08-11 (seat launchers
# were outside the cron-referenced protected set). See experiment-factory skill.
set -u
LAUNCH_EXPERIMENT=/home/frank/.hermes/scripts/experiment_swarm.sh
LAUNCH_DISCOVERY=/home/frank/.hermes/scripts/discovery_swarm.sh
# liveness: both launchers must exist + be executable (self-heal target if not)
for f in "$LAUNCH_EXPERIMENT" "$LAUNCH_DISCOVERY"; do
  [ -x "$f" ] || echo "WARN experiment-factory launcher missing/non-exec: $f"
done
echo "experiment-factory cadence tick $(date -u +%FT%TZ): launchers present=$([ -x "$LAUNCH_EXPERIMENT" ] && [ -x "$LAUNCH_DISCOVERY" ] && echo yes || echo NO)"
exit 0
