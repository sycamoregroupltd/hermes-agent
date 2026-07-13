#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# spine-liveness-watch — no-agent seat-liveness + cron-topology watchdog.
# Wraps the read-only multi-day-spine-audit.py and pages Frank (telegram) ONLY when
# the audit verdict / WARN+CRITICAL finding-set CHANGES (two-tier only-on-material-
# change; silent while unchanged). Deterministic, no LLM, no mutation.
set -uo pipefail
AUDIT=/home/frank/obsidian-fleet-vault/Orchestration/sessions/bin/multi-day-spine-audit.py
STATE=/home/frank/.hermes/cron/state/spine-liveness-watch.sig
mkdir -p "$(dirname "$STATE")"

OUT="$(python3 "$AUDIT" 2>/dev/null)" || { echo "spine-liveness-watch: audit failed to run"; exit 0; }
VERDICT="$(printf '%s\n' "$OUT" | grep -m1 '^Verdict:' | awk '{print $2}')"
SIG="$(printf '%s\n' "$OUT" | grep -E '^Verdict:|^- (WARN|CRITICAL|FAIL)' | md5sum | awk '{print $1}')"
PREV="$(cat "$STATE" 2>/dev/null || true)"

# unchanged since last run -> stay silent (no alert fatigue)
[ "$SIG" = "$PREV" ] && exit 0
printf '%s' "$SIG" > "$STATE"

if [ "${VERDICT:-UNKNOWN}" = "READY" ]; then
  echo "✅ Multi-day spine: recovered to READY"
  exit 0
fi
echo "⚠️ Multi-day spine watch: ${VERDICT:-UNKNOWN} (state changed)"
printf '%s\n' "$OUT" | grep -E '^- (WARN|CRITICAL|FAIL)' | head -12
echo "(full report: obsidian-fleet-vault/Orchestration/sessions/activity/multi-day-spine-audit-latest.md)"

