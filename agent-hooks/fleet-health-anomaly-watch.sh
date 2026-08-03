#!/usr/bin/env bash
# fleet-health-anomaly-watch — no-agent deterministic gate for fleet anomalies.
# Detects: high consecutive_failures on tasks, recent crash storms, stalled running tasks.
# Emits wakeAgent:true only on NEW anomaly (state-tracked). Silent/fail-open otherwise.
# Follows crashstorm-watch + dgx-health-watch patterns. Non-destructive, read-only.
set -uo pipefail

BOARDS="upero jarvis-os sycode-ai sycode-trading yorkstone-supplies"
THRESHOLD_CONSEC="${FLEET_ANOMALY_CONSEC_THRESHOLD:-3}"
THRESHOLD_CRASH="${FLEET_ANOMALY_CRASH_THRESHOLD:-4}"
WINDOW_SECONDS="${FLEET_ANOMALY_WINDOW_SECONDS:-3600}"
STATE_DIR="${FLEET_ANOMALY_STATE_DIR:-/home/frank/.hermes/cron/state}"
STATE_FILE="${STATE_DIR}/fleet-health-anomaly.seen"
mkdir -p "$STATE_DIR" 2>/dev/null || { echo '{"wakeAgent": false}'; exit 0; }
touch "$STATE_FILE" 2>/dev/null || { echo '{"wakeAgent": false}'; exit 0; }

now=$(date +%s 2>/dev/null || echo 0)
[ "$now" -gt 0 ] || { echo '{"wakeAgent": false}'; exit 0; }
since=$((now - WINDOW_SECONDS))

signals=""
for b in $BOARDS; do
  db="/home/frank/.hermes/kanban/boards/$b/kanban.db"
  [ -r "$db" ] || continue

  # 1. High consecutive_failures (stalled/circuit-broken tasks)
  consec=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE consecutive_failures >= $THRESHOLD_CONSEC AND status NOT IN ('done','archived')" 2>/dev/null || echo 0)
  consec=${consec:-0}
  case "$consec" in ''|*[!0-9]*) consec=0 ;; esac
  if [ "$consec" -ge 1 ]; then
    sample=$(sqlite3 -separator ' ' "$db" "SELECT id || ':' || assignee || '(fails=' || consecutive_failures || ')' FROM tasks WHERE consecutive_failures >= $THRESHOLD_CONSEC AND status NOT IN ('done','archived') ORDER BY consecutive_failures DESC LIMIT 3" 2>/dev/null | tr '\n' ';' | sed 's/["\\]/_/g' || true)
    signals="${signals}${b}:HIGH_CONSEC_FAILS(count=${consec},thresh=${THRESHOLD_CONSEC},sample=${sample}) "
  fi

  # 2. Crash storm (reuse threshold)
  crash=$(sqlite3 "$db" "SELECT COUNT(*) FROM task_runs WHERE status IN ('crashed','timed_out') AND COALESCE(ended_at,started_at,0) >= $since" 2>/dev/null || echo 0)
  crash=${crash:-0}
  case "$crash" in ''|*[!0-9]*) crash=0 ;; esac
  if [ "$crash" -ge "$THRESHOLD_CRASH" ]; then
    sample=$(sqlite3 -separator ' ' "$db" "SELECT task_id || ':' || COALESCE(substr(error,1,40),'no-err') FROM task_runs WHERE status IN ('crashed','timed_out') AND COALESCE(ended_at,started_at,0) >= $since ORDER BY COALESCE(ended_at,started_at) DESC LIMIT 3" 2>/dev/null | tr '\n' ';' | sed 's/["\\]/_/g' || true)
    signals="${signals}${b}:CRASHSTORM(count=${crash},window=${WINDOW_SECONDS}s,sample=${sample}) "
  fi

  # 3. Stalled running tasks (long-running without progress)
  stalled=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status='running' AND started_at > 0 AND ( $now - started_at ) > $WINDOW_SECONDS" 2>/dev/null || echo 0)
  stalled=${stalled:-0}
  case "$stalled" in ''|*[!0-9]*) stalled=0 ;; esac
  if [ "$stalled" -ge 1 ]; then
    sample=$(sqlite3 -separator ' ' "$db" "SELECT id || ':' || assignee || '(' || ( $now - started_at ) || 's)' FROM tasks WHERE status='running' AND started_at > 0 AND ( $now - started_at ) > $WINDOW_SECONDS ORDER BY started_at ASC LIMIT 3" 2>/dev/null | tr '\n' ';' | sed 's/["\\]/_/g' || true)
    signals="${signals}${b}:STALLED_RUNNING(count=${stalled},window=${WINDOW_SECONDS}s,sample=${sample}) "
  fi
done

if [ -n "$signals" ]; then
  sig=$(printf '%s' "$signals" | md5sum | cut -c1-12 2>/dev/null || printf '%s' "$signals")
  if grep -qxF "$sig" "$STATE_FILE" 2>/dev/null; then
    echo "fleet-health-anomaly-watch: condition persists (already woke): $signals"
    echo '{"wakeAgent": false}'
  else
    echo "$sig" >> "$STATE_FILE" 2>/dev/null || true
    echo "WAKE NERVOUS-SYSTEM — fleet anomaly: $signals"
    echo '{"wakeAgent": true}'
  fi
else
  : > "$STATE_FILE" 2>/dev/null || true
  echo "fleet-health-anomaly-watch: no anomalies (consec>=${THRESHOLD_CONSEC}, crashes>=${THRESHOLD_CRASH}, stalled>=1 in ${WINDOW_SECONDS}s)"
  echo '{"wakeAgent": false}'
fi
