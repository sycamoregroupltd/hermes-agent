#!/usr/bin/env bash
# gate-pm-landing.sh — pre_tool_call hook for PM / LANDING profiles.
#
# Containment for the exec grant made under t_ecf1d553. PM seats are granted `terminal`
# because /home/frank/uaa-rules/delegated-authority.md:6-7 assigns them trunk-landing duty
# and their SOULs mandate `git push` / merge verification. This gate makes the grant safe by
# enforcing, at runtime, the graduated push rule those SOULs already state in prose
# ("Pushing to our repos — don't false-block", 2026-06-15):
#   feature-branch push = allowed | main/master push = blocked, surface to Frank
#   NousResearch/* = blocked (no write access) | force/delete push = blocked
# Local `git add`/`git commit` stay ALLOWED — unlike a critic, a PM must commit before
# kanban_complete or the worktree is reaped and the artifact is lost.
#
# FAIL-OPEN — never wedge the fleet.
# Bypass: ALLOW_PM_TRUNK_PUSH=1 (orchestrator/Frank-approved landing only).
set -uo pipefail || true
LOG=/home/frank/.hermes/cron/state/pm-landing-gate.log
PY=/home/frank/.hermes/agent-hooks/gate-pm-landing.py
payload=$(cat 2>/dev/null)

if [ "${ALLOW_PM_TRUNK_PUSH:-}" = "1" ]; then
  printf '%s ALLOW(bypass) ALLOW_PM_TRUNK_PUSH=1\n' "$(date -u +%FT%TZ)" >> "$LOG" 2>/dev/null || true
  echo '{}'; exit 0
fi

out=$(printf '%s' "$payload" | python3 "$PY" 2>/dev/null)
[ -n "$out" ] && printf '%s\n' "$out" || echo '{}'
exit 0
