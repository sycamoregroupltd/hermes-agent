#!/usr/bin/env bash
# grok-session-end-recap.sh — SessionEnd observer.
# Dry-run by default. Set GROK_SESSION_RECAP_WRITE=1 to write memory stubs.
# Never LLM-calls. Fail-open. Skip Hermes cells / subagents / trivial sessions.
set -uo pipefail

LOG_DIR="${GROK_HOME:-$HOME/.grok}/logs"
MEM_ROOT="${GROK_HOME:-$HOME/.grok}/memory"
SESS_ROOT="${GROK_HOME:-$HOME/.grok}/sessions"
LOG="$LOG_DIR/session-end-recap.jsonl"
WRITE="${GROK_SESSION_RECAP_WRITE:-0}"
mkdir -p "$LOG_DIR"

payload="$(cat || true)"
session_id="${GROK_SESSION_ID:-}"
cwd=""
kind=""
title=""
recap=""
msgs=0

if command -v jq >/dev/null 2>&1 && [ -n "$payload" ]; then
  session_id="$(printf '%s' "$payload" | jq -r '.sessionId // empty')"
  cwd="$(printf '%s' "$payload" | jq -r '.cwd // .workspaceRoot // empty')"
  kind="$(printf '%s' "$payload" | jq -r '.sessionKind // .subagentType // empty')"
fi
session_id="${session_id:-$GROK_SESSION_ID}"

summary=""
if [ -n "$session_id" ]; then
  summary="$(find "$SESS_ROOT" -mindepth 2 -maxdepth 3 -type d -name "$session_id" 2>/dev/null | head -1)"
fi
if [ -n "$summary" ] && [ -f "$summary/summary.json" ] && command -v jq >/dev/null 2>&1; then
  title="$(jq -r '.generated_title // .session_summary // empty' "$summary/summary.json")"
  recap="$(jq -r '.last_recap // .last_turn_summary // empty' "$summary/summary.json")"
  msgs="$(jq -r '.num_messages // 0' "$summary/summary.json")"
  kind="${kind:-$(jq -r '.session_kind // empty' "$summary/summary.json")}"
  cwd="${cwd:-$(jq -r '.info.cwd // .git_root_dir // empty' "$summary/summary.json")}"
fi

skip=""
case "$kind" in
  subagent|subagent_fork) skip="kind:$kind" ;;
esac
printf '%s' "$title" | grep -Eqi 'DGX Fleet Fallback|grok work-cell|GROK-ARM|You are a grok work-cell' \
  && skip="${skip:-title-cell}"
if [ "${msgs:-0}" -lt 6 ] 2>/dev/null; then
  skip="${skip:-trivial-msgs}"
fi

ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
day="$(date -u +%Y-%m-%d)"
action="DRY-RUN"
if [ -n "$skip" ]; then
  action="SKIP"
elif [ "$WRITE" = "1" ]; then
  action="WRITE"
fi

# Redact anything that looks like a token in the log line (title/recap only, clipped).
clip() { printf '%s' "$1" | tr '\n' ' ' | cut -c1-160; }

printf '%s\n' "{\"ts\":\"$ts\",\"action\":\"$action\",\"skip\":\"$skip\",\"session\":\"$session_id\",\"msgs\":$msgs,\"kind\":\"$kind\",\"title\":\"$(clip "$title")\"}" >>"$LOG"

if [ "$action" != "WRITE" ]; then
  exit 0
fi

# Workspace memory dir: prefer existing hashed folder; else frank-home fallback.
ws="$(ls -d "$MEM_ROOT"/*/sessions 2>/dev/null | head -1)"
if [ -n "$ws" ]; then
  dest_dir="$(dirname "$ws")"
else
  dest_dir="$MEM_ROOT"
fi
mkdir -p "$dest_dir/sessions"
out="$dest_dir/sessions/${day}-${session_id:-unknown}.md"
{
  echo "# Session stub ${day}"
  echo
  echo "- title: $(clip "$title")"
  echo "- recap: $(clip "$recap")"
  echo "- cwd: $cwd"
  echo "- session: $session_id"
  echo
  echo "Cache only. Durable Sycode notes go in obsidian/sycode-trading/learnings/."
  echo "Do not treat this as live board/SHA/position state."
} >"$out"
exit 0
