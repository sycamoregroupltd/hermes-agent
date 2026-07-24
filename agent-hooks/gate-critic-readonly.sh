#!/usr/bin/env bash
# gate-critic-readonly.sh — pre_tool_call hook for CRITIC/REVIEWER profiles.
# Hard guarantee that a reviewer never mutates the artifact under review (no code edit, git commit/
# push, or in-place file mutation) while staying able to read, run read-only verification, post
# kanban verdicts, and write review notes to the Obsidian vault. FAIL-OPEN — never wedge the fleet.
# Bypass: ALLOW_CRITIC_WRITE=1 (orchestrator/dispatcher only) for an explicitly-approved repair.
set -uo pipefail || true
LOG=/home/frank/.hermes/cron/state/critic-readonly-gate.log
PY=/home/frank/.hermes/agent-hooks/gate-critic-readonly.py
payload=$(cat 2>/dev/null)

if [ "${ALLOW_CRITIC_WRITE:-}" = "1" ]; then
  printf '%s ALLOW(bypass) ALLOW_CRITIC_WRITE=1\n' "$(date -u +%FT%TZ)" >> "$LOG" 2>/dev/null || true
  echo '{}'; exit 0
fi

out=$(printf '%s' "$payload" | python3 "$PY" 2>/dev/null)
[ -n "$out" ] && printf '%s\n' "$out" || echo '{}'
exit 0

