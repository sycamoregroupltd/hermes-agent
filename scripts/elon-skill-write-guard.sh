#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
set -euo pipefail

STATE_DIR="/home/frank/.hermes/profiles/jarvis/cron/state"
STATE_FILE="$STATE_DIR/elon-skill-write-guard.state"
LOG_FILE="/home/frank/.hermes/profiles/jarvis/logs/agent.log"
ERROR_LOG="/home/frank/.hermes/profiles/jarvis/logs/errors.log"
SKILL_ROOTS=(
  "/home/frank/.hermes/profiles/jarvis/skills"
  "/home/frank/.hermes/profiles/elon/skills"
)
WATCH_IDS=(
  "cron_e51c9e2fa5df"   # elon-governance-loop
  "cron_46b4535eb511"   # elon-live-activity-report
  "cron_e435af190e9e"   # upero-pm-governance (caught skill_manage during governor churn)
)
mkdir -p "$STATE_DIR"

declare -A OFFSETS
if [[ -f "$STATE_FILE" ]]; then
  while IFS='=' read -r key val; do
    [[ -n "${key:-}" ]] || continue
    OFFSETS[$key]="$val"
  done < "$STATE_FILE"
fi

violations=()
scan_log() {
  local key="$1" path="$2"
  [[ -f "$path" ]] || return 0
  local size prev tmp
  size=$(stat -c '%s' "$path" 2>/dev/null || echo 0)
  prev="${OFFSETS[$key]:-0}"
  if ! [[ "$prev" =~ ^[0-9]+$ ]] || (( prev > size )); then
    prev=0
  fi
  tmp=$(mktemp)
  tail -c +$((prev + 1)) "$path" > "$tmp" || true
  while IFS= read -r line; do
    for id in "${WATCH_IDS[@]}"; do
      if [[ "$line" == *"$id"* && "$line" == *"skill_manage"* ]]; then
        violations+=("${path##*/}: $line")
      fi
    done
  done < "$tmp"
  rm -f "$tmp"
  OFFSETS[$key]="$size"
}

scan_log agent "$LOG_FILE"
scan_log errors "$ERROR_LOG"

# Detect direct skill file mutations under the Jarvis-hosted Elon topology.
# State key is the absolute SKILL.md path; value is mtime:size.
for root in "${SKILL_ROOTS[@]}"; do
  [[ -d "$root" ]] || continue
  while IFS= read -r -d '' skill; do
    stat_pair="$(stat -c '%Y:%s' "$skill" 2>/dev/null || true)"
    [[ -n "$stat_pair" ]] || continue
    prior="${OFFSETS[$skill]:-}"
    if [[ -n "$prior" && "$prior" != "$stat_pair" ]]; then
      violations+=("SKILL_FILE_CHANGED: $skill $prior -> $stat_pair")
    fi
    OFFSETS[$skill]="$stat_pair"
  done < <(find "$root" -path '*/SKILL.md' -type f -print0)
done

{
  for key in "${!OFFSETS[@]}"; do
    printf '%s=%s\n' "$key" "${OFFSETS[$key]}"
  done | sort
} > "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"

if (( ${#violations[@]} > 0 )); then
  printf 'ELON_SKILL_WRITE_GUARD violation(s): %d\n' "${#violations[@]}"
  printf '%s\n' "${violations[@]}" | sed -n '1,12p'
fi
