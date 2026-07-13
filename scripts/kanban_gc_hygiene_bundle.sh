#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Wrapper: delegates to the jarvis-profile GC hygiene apply script.
# Installed at ~/.hermes/scripts/ so hermes cron --no-agent --script can reach it.
set -euo pipefail
export KANBAN_GC_HYGIENE_APPLY=1
exec python3 /home/frank/.hermes/profiles/jarvis/scripts/kanban_gc_hygiene_bundle_apply.py
