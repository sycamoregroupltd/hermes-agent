#!/usr/bin/env bash
# kanban_dashboard_liveness_heal.sh — Hermes-native heal for hermes-kanban-dashboard :9127
# WHY (2026-09-06 native-improve pulse): unit sat inactive/dead since 2026-09-03 22:05 BST
# (clean TERM; Restart=on-failure did not recover). hermes-surface-gateway stormed
# ECONNREFUSED 127.0.0.1:9127 every 5s while SQLite fallback masked the outage.
# devops/scripts/alerts/dashboard-liveness.sh is observe-only + WhatsApp — Isolation
# forbids auto WhatsApp. This job is no_agent: probe + start only, log throttle, local deliver.
#
# NEVER: hermes update, raise CI CAP, WhatsApp/money/A3/Deribit, start ci-3..9.
set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME="${HERMES_HOME:-/home/frank/.hermes}"

URL="${KANBAN_DASH_URL:-http://127.0.0.1:9127/api/status}"
UNIT="${KANBAN_DASH_UNIT:-hermes-kanban-dashboard.service}"
LOG_FILE="${KANBAN_DASH_LOG:-$HERMES_HOME/logs/kanban-dashboard-liveness.log}"
STATE_FILE="${KANBAN_DASH_STATE:-$HERMES_HOME/state/kanban-dashboard-liveness-state.txt}"
REALERT_SECS="${KANBAN_DASH_REALERT_SECS:-3600}"
CURL_MAX="${KANBAN_DASH_CURL_MAX:-5}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")"
now_epoch=$(date +%s)
now_iso=$(date -Is)
log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

probe() {
  curl -sf --max-time "$CURL_MAX" -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null || echo "000"
}

code=$(probe)
if [ "$code" = "200" ]; then
  # healthy: silent for --no-agent (empty stdout)
  exit 0
fi

unit_state=$(systemctl --user is-active "$UNIT" 2>/dev/null || echo unknown)
log "RED http=$code unit=$unit_state — attempting start"

# throttle repeated start storms
last=$(grep -a '^last_heal=' "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2 || true)
if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt 60 ]; then
  log "SUPPRESS heal (within 60s of last)"
  echo "kanban-dashboard RED http=$code unit=$unit_state heal_suppressed"
  exit 0
fi

systemctl --user start "$UNIT" >/dev/null 2>&1 || true
sleep 2
code2=$(probe)
unit2=$(systemctl --user is-active "$UNIT" 2>/dev/null || echo unknown)
grep -av '^last_heal=' "$STATE_FILE" 2>/dev/null > "$STATE_FILE.tmp" || true
echo "last_heal=$now_epoch" >> "$STATE_FILE.tmp"
echo "last_http=$code2" >> "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"

if [ "$code2" = "200" ]; then
  log "HEALED http=$code2 unit=$unit2"
  # fleet card optional, non-fatal, no WhatsApp
  if [ -x "$HERMES_HOME/scripts/fleet-alert-card.sh" ]; then
    last_card=$(grep -a '^last_card=' "$STATE_FILE" 2>/dev/null | tail -1 | cut -d= -f2 || true)
    if [ -z "${last_card:-}" ] || [ $((now_epoch - last_card)) -ge "$REALERT_SECS" ]; then
      "$HERMES_HOME/scripts/fleet-alert-card.sh" "kanban-dashboard-liveness" \
        "[HEALED] kanban dashboard :9127" \
        "Was http=$code unit=$unit_state; after start http=$code2 unit=$unit2" >/dev/null 2>&1 || true
      echo "last_card=$now_epoch" >> "$STATE_FILE"
    fi
  fi
  echo "kanban-dashboard HEALED http=$code->$code2 unit=$unit_state->$unit2"
  exit 0
fi

log "STILL_RED http=$code2 unit=$unit2"
echo "kanban-dashboard STILL_RED http=$code2 unit=$unit2 (was $code/$unit_state)"
exit 0
