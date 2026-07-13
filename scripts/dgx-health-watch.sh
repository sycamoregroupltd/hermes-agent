#!/usr/bin/env bash
# dgx-health-watch — emit a DGX health alert to stdout ONLY when overall
# health TRANSITIONS (ok <-> warn <-> down). Empty output = no change = silent.
#
# Designed for the deterministic Hermes cron watchdog pattern:
#   hermes cron create "every 10m" --name dgx-health-watch \
#     --no-agent --deliver telegram --script dgx-health-watch.sh
#
# With --no-agent, this script's stdout is delivered verbatim and empty
# stdout is suppressed — so you only hear from it when something changes.
set -uo pipefail

STATUS_BIN="${DGX_STATUS_BIN:-/home/frank/bin/dgx-status}"
STATE_FILE="${DGX_WATCH_STATE:-/home/frank/.hermes/.dgx-health.state}"

# coarse verdict: ok | warn | down | unknown
verdict=$("$STATUS_BIN" --json 2>/dev/null | jq -r '.summary // "unknown"' 2>/dev/null)
[ -z "$verdict" ] && verdict="unknown"

prev=$(cat "$STATE_FILE" 2>/dev/null || echo "init")
echo "$verdict" > "$STATE_FILE" 2>/dev/null || true

# silent when nothing changed
[ "$verdict" = "$prev" ] && exit 0
# don't announce a healthy first-ever run (avoid startup noise)
[ "$prev" = "init" ] && [ "$verdict" = "ok" ] && exit 0

# gather the offending lines (skip the informational swap line + the cron note)
detail=$("$STATUS_BIN" --no-color 2>/dev/null \
  | grep -E '✗|⚠' | grep -vE 'informational|wakeAgent' | sed 's/^[[:space:]]*//')

case "$verdict" in
  ok)   echo "✅ DGX recovered — all systems healthy (was: ${prev})" ;;
  warn) echo "⚠️ DGX degraded — warning(s) present (was: ${prev})" ;;
  down) echo "🔴 DGX ALERT — component DOWN (was: ${prev})" ;;
  *)    echo "❔ DGX status: ${verdict} (was: ${prev})" ;;
esac
[ -n "$detail" ] && echo "$detail"
echo "— $(date '+%F %T') $(hostname) · run 'dgx-status' for full view"
