#!/usr/bin/env bash
# Emit a minimal Claude Code lifecycle signal to the guarded Hermes Session Bus.
# This hook is intentionally a notifier, not an executor: it never resumes a
# model, submits a prompt, changes a task, or performs an action in a project.

set -u -o pipefail

BUS_BIN="${SESSION_BUS_BIN:-/home/frank/obsidian-fleet-vault/Orchestration/sessions/bin/session-bus.sh}"
AUTHOR="${CLAUDE_EVENT_AUTHOR:-claude-code}"
INPUT="$(cat 2>/dev/null || true)"

json_value() {
  local filter="$1"
  printf '%s' "$INPUT" | jq -r "$filter" 2>/dev/null || true
}

event_name="$(json_value '.hook_event_name // "unknown"')"
session_id="$(json_value '.session_id // "unknown"')"
reason="$(json_value '.reason // .notification_type // ""')"
cwd="$(json_value '.cwd // ""')"

# Keep the shared event stream small, path-safe, and secret-free. The canonical
# transcript remains provider-private; the bus gets only an opaque identifier.
session_id="$(printf '%s' "$session_id" | tr -cd 'A-Za-z0-9._-' | cut -c1-96)"
event_name="$(printf '%s' "$event_name" | tr -cd 'A-Za-z0-9._-' | cut -c1-48)"
reason="$(printf '%s' "$reason" | tr '\n\r' ' ' | tr -cd '[:alnum:] .,_:-' | cut -c1-160)"
cwd_name="$(basename "${cwd:-unknown}" | tr -cd 'A-Za-z0-9._-' | cut -c1-96)"

if [[ -z "$session_id" ]]; then session_id="unknown"; fi
if [[ -z "$event_name" ]]; then event_name="unknown"; fi
if [[ -z "$cwd_name" ]]; then cwd_name="unknown"; fi

case "$event_name" in
  Stop)
    kind="CLAUDE_TURN_STOP"
    ;;
  Notification)
    kind="CLAUDE_ATTENTION"
    ;;
  SessionEnd)
    kind="CLAUDE_SESSION_END"
    ;;
  *)
    kind="CLAUDE_EVENT"
    ;;
esac

message="$kind session=$session_id workspace=$cwd_name event=$event_name"
if [[ -n "$reason" ]]; then message="$message reason=$reason"; fi

# The event bridge permits only the fixed orchestrator-sync card and rejects
# secrets/approval payloads. A telemetry failure must never block Claude's stop.
if [[ -x "$BUS_BIN" ]]; then
  "$BUS_BIN" event --author "$AUTHOR" --text "$message" >/dev/null 2>&1 || true
elif command -v ssh >/dev/null 2>&1; then
  # Mac Claude Code sessions route to the same canonical DGX bus. All fields
  # above are deliberately restricted before interpolation into this command.
  ssh -o BatchMode=yes -o ConnectTimeout=5 dgx \
    "$BUS_BIN event --author '$AUTHOR' --text '$message'" >/dev/null 2>&1 || true
fi

# Avoid transcript clutter. Hooks still succeed if jq is unavailable.
if command -v jq >/dev/null 2>&1; then
  jq -n '{continue:true,suppressOutput:true}'
fi
exit 0
