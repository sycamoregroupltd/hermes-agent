#!/usr/bin/env bash
# Detects a Hermes version change since last audit. Emits a notification ONLY
# when the installed git HEAD changes - the trigger for the native-improvement audit.
# Quiet (empty stdout) when nothing changed (no-agent cron delivers nothing).
set -uo pipefail
REPO="$HOME/.hermes/hermes-agent"
STATE="$HOME/.hermes/.last-improvement-audit-ref"
cur=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo "unknown")
ver=$(cd "$REPO" && git describe --tags --always 2>/dev/null || echo "?")
last=$(cat "$STATE" 2>/dev/null || echo "")
if [ -z "$last" ]; then
  echo "$cur" > "$STATE"
  exit 0
fi
if [ "$cur" != "$last" ]; then
  printf 'HERMES UPDATED: %s -> %s (%s).\nRun the native-improvement finder to map new features (say "look for hermes improvements", or run the hermes-native-improvement-finder workflow with sinceRef=%s).\n' "$last" "$cur" "$ver" "$last"
  echo "$cur" > "$STATE"
fi
exit 0
