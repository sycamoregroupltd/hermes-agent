#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook §Canonical Script Copy Rule.
# Invoker: Jarvis cron job `native-fleet-heartbeat` (id df22a92505cb, no_agent, daily 0 8 * * *).
# Profile-local copy at ~/.hermes/profiles/jarvis/scripts/fleet_heartbeat_check.sh is an approved exec SHIM forwarding here.
#
# Pure local health probe (disk/load/cron) + Discord delivery with RETRY so a
# transient DNS/network blip does NOT fail the run, and we do NOT spam on blips
# (single best-effort delivery attempt per run; retry is in-process only).
set -uo pipefail

TS=$(date -u +%Y-%m-%dT%H:%MZ)
TIMEOUT_BIN=${TIMEOUT_BIN:-/usr/bin/timeout}
HERMES_BIN=${HERMES_BIN:-/home/frank/.local/bin/hermes}

run_bounded() {
  local label=$1
  local limit=$2
  shift 2
  local out rc
  out=$("$TIMEOUT_BIN" --kill-after=5s "$limit" "$@" 2>&1)
  rc=$?
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    printf '%s timed out after %s\n' "$label" "$limit"
    return 124
  fi
  if [ "$rc" -ne 0 ]; then
    printf '%s failed rc=%s: %s\n' "$label" "$rc" "$out"
    return "$rc"
  fi
  printf '%s\n' "$out"
  return 0
}

# --- 1. Local health probe (no external calls) ---
DISK_RAW=$(run_bounded "df -h /" 15s df -h /)
if [ $? -eq 0 ]; then
  DISK=$(printf '%s\n' "$DISK_RAW" | awk 'NR==2 {print $5" used "$3" of "$2" ("$4" avail)"}')
else
  DISK="$DISK_RAW"
fi

LOAD_RAW=$(run_bounded "uptime" 15s uptime)
if [ $? -eq 0 ]; then
  LOAD=$(printf '%s\n' "$LOAD_RAW" | sed 's/.*load average: //')
else
  LOAD="$LOAD_RAW"
fi

CRON_LIST=$(HERMES_HOME=/home/frank/.hermes/profiles/jarvis run_bounded "hermes cron list" 45s "$HERMES_BIN" cron list)
CRON_RC=$?
if [ "$CRON_RC" -eq 0 ]; then
  CRON_ERRS=$(printf '%s\n' "$CRON_LIST" | grep -iE "error:" | grep -v "deliver" | head -10 || true)
else
  CRON_ERRS="$CRON_LIST"
fi

BODY="FLEET HEARTBEAT $TS
Disk /: $DISK
Load: $LOAD"
if [ -n "$CRON_ERRS" ]; then
  BODY="$BODY

Cron jobs with errors:
$CRON_ERRS"
else
  BODY="$BODY

Cron: all listed jobs healthy (or see delivery notes)."
fi

# --- 2. Discord delivery with retry (no spam on transient blips) ---
WEBHOOK="${FLEET_HEARTBEAT_WEBHOOK:-}"
if [ -z "$WEBHOOK" ]; then
  # No webhook configured -> still produce the report (delivered to cron target).
  echo "$BODY"
  echo "(no FLEET_HEARTBEAT_WEBHOOK set; report not pushed to Discord)"
  exit 0
fi

# In-process retry: transient 'Temporary failure in name resolution' is handled
# by curl's --retry; we do NOT loop per-run so a hard outage won't spam.
DELIVERED=0
for attempt in 1 2 3; do
  if curl -fsS --retry 3 --retry-delay 2 --retry-all-errors --max-time 20 \
     -H "Content-Type: application/json" \
     -d "$(python3 -c 'import json,sys; print(json.dumps({"content": sys.argv[1]}))' "$BODY")" \
     "$WEBHOOK" 2>/dev/null; then
    DELIVERED=1
    break
  fi
  sleep 2
done

if [ $DELIVERED -eq 1 ]; then
  echo "Fleet heartbeat delivered to Discord at $TS"
else
  # Delivery failed after retries -> report locally so the run still shows the
  # health data (do not silently drop). Cron local delivery captures this.
  echo "$BODY"
  echo "(Discord delivery failed after retries at $TS; health data above)"
fi
