#!/usr/bin/env bash
# Crashstorm watch — no-agent wakeAgent gate.
# Scans accumulated kanban task_runs for recent crash storms that are only
# visible in DB state, not a single tool event. Fail-closed-to-silent:
# any uncertainty emits {"wakeAgent":false}.
set -uo pipefail

BOARDS="upero jarvis-os sycode-ai sycode-trading"
# The external failure-class detector woke on crashstorm:<board>:4. Treat four
# abnormal worker exits in an hour as a storm so the standing loop covers the
# observed class exactly; callers can override for tests or tighter boards.
THRESHOLD="${CRASHSTORM_THRESHOLD:-4}"
WINDOW_SECONDS="${CRASHSTORM_WINDOW_SECONDS:-3600}"
STATE="${CRASHSTORM_STATE:-/home/frank/.hermes/cron/state/crashstorm-watch.seen}"
mkdir -p "$(dirname "$STATE")" 2>/dev/null || { echo '{"wakeAgent": false}'; exit 0; }
touch "$STATE" 2>/dev/null || { echo '{"wakeAgent": false}'; exit 0; }

now=$(date +%s 2>/dev/null || echo 0)
[ "$now" -gt 0 ] || { echo '{"wakeAgent": false}'; exit 0; }
since=$((now - WINDOW_SECONDS))

signals=""
for b in $BOARDS; do
  db="/home/frank/.hermes/kanban/boards/$b/kanban.db"
  [ -r "$db" ] || continue
  count=$(sqlite3 "$db" "SELECT COUNT(*) FROM task_runs WHERE status IN ('crashed','timed_out') AND COALESCE(ended_at,started_at,0) >= $since" 2>/dev/null || echo 0)
  count=${count:-0}
  case "$count" in ''|*[!0-9]*) count=0 ;; esac
  if [ "$count" -ge "$THRESHOLD" ]; then
    sample=$(sqlite3 -separator ' ' "$db" "SELECT task_id || ':' || COALESCE(substr(error,1,60),'no-error') FROM task_runs WHERE status IN ('crashed','timed_out') AND COALESCE(ended_at,started_at,0) >= $since ORDER BY COALESCE(ended_at,started_at) DESC LIMIT 3" 2>/dev/null | tr '\n' ';' | sed 's/["\\]/_/g' || true)
    signals="${signals}${b}:CRASHSTORM(count=${count},window=${WINDOW_SECONDS}s,sample=${sample}) "
  fi
done

if [ -n "$signals" ]; then
  sig=$(printf '%s' "$signals" | md5sum | cut -c1-12 2>/dev/null || printf '%s' "$signals")
  if grep -qxF "$sig" "$STATE" 2>/dev/null; then
    echo "crashstorm-watch: condition persists (already woke): $signals"
    echo '{"wakeAgent": false}'
  else
    echo "$sig" >> "$STATE" 2>/dev/null || true
    echo "WAKE NERVOUS-SYSTEM — crashstorm condition(s): $signals"
    echo '{"wakeAgent": true}'
  fi
else
  : > "$STATE" 2>/dev/null || true
  echo "crashstorm-watch: no board has >=${THRESHOLD} crashed/timed_out task_runs in ${WINDOW_SECONDS}s"
  echo '{"wakeAgent": false}'
fi
