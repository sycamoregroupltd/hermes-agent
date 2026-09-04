#!/usr/bin/env bash
# seat-live-state-hook.sh — SessionStart hook wrapper around seat-live-state.sh.
#
# CONSUMED BY: the SessionStart hook in ~/.claude/settings.json (this is its command).
# Purpose: run the seat snapshot and emit it as `additionalContext` JSON so it lands in
#   the MODEL's context (not user-facing stdout — keeps every session clean), with
#   suppressOutput so it never clutters the transcript. If the snapshot cannot generate,
#   inject an explicit UNAVAILABLE banner (fail-loud: absence of a snapshot = assume
#   stale, never assume fine). Total hard cap so a wedged probe can't gate session start.
set -uo pipefail

# Opportunistic background refresh of the deep dashboard (STATE.md), boot-driven per the
# chosen surface — NON-BLOCKING, and only when STATE.md is missing or >10min stale, so
# back-to-back sessions don't re-reconcile. Detached so it can't gate session start.
SMD="/home/frank/obsidian/sycode-trading/STATE.md"
if [ ! -f "$SMD" ] || [ $(( $(date +%s) - $(stat -c %Y "$SMD" 2>/dev/null || echo 0) )) -gt 600 ]; then
  ( nohup python3 /home/frank/.hermes/scripts/reconcile-state.py >/dev/null 2>&1 & ) 2>/dev/null || true
fi

SNAP="$(timeout 12 bash /home/frank/.hermes/scripts/seat-live-state.sh 2>/dev/null)"
if [ -z "$SNAP" ]; then
  SNAP="━━ SEAT LIVE-STATE: SNAPSHOT UNAVAILABLE ━━
The boot snapshot could not generate. Treat ALL live-state as UNKNOWN and VERIFY before
asserting or acting. Re-run manually: bash ~/.hermes/scripts/seat-live-state.sh"
fi

if command -v jq >/dev/null 2>&1; then
  jq -n --arg c "$SNAP" \
    '{suppressOutput:true, hookSpecificOutput:{hookEventName:"SessionStart", additionalContext:$c}}'
else
  # jq unavailable — SessionStart still injects raw stdout into context as a fallback.
  printf '%s\n' "$SNAP"
fi
exit 0
