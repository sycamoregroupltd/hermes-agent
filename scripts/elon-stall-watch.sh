#!/usr/bin/env bash
# Elon stall-watch — documented Hermes wakeAgent SQL-count gate (5-min poll).
# Wakes Elon's full LLM cycle ONLY when a board is genuinely stuck. State-deduped
# so the same condition doesn't re-wake every tick. Final line = {"wakeAgent":...}.
set -uo pipefail
BOARDS="upero jarvis-os sycode-ai sycode-trading"
STATE="/home/frank/.hermes/cron/state/elon-stall-watch.seen"
mkdir -p "$(dirname "$STATE")"; touch "$STATE"

signals=""
for b in $BOARDS; do
  db="/home/frank/.hermes/kanban/boards/$b/kanban.db"
  [ -f "$db" ] || { signals="${signals}${b}:MISSING-DB "; continue; }
  blocked=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status='blocked'" 2>/dev/null || echo 0)
  ready=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status IN ('todo','ready')" 2>/dev/null || echo 0)
  running=$(sqlite3 "$db" "SELECT COUNT(*) FROM task_runs WHERE status='running'" 2>/dev/null || echo 0)
  done24=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at > strftime('%s','now','-1 day')" 2>/dev/null || echo 0)
  fails=$(sqlite3 "$db" "SELECT COALESCE(MAX(consecutive_failures),0) FROM tasks WHERE status NOT IN ('done','archived')" 2>/dev/null || echo 0)
  orphan=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status IN ('todo','ready') AND (assignee IS NULL OR assignee='')" 2>/dev/null || echo 0)
  blocked=${blocked:-0}; ready=${ready:-0}; running=${running:-0}; done24=${done24:-0}; fails=${fails:-0}; orphan=${orphan:-0}

  # CONDITION A — IDLE: ready work waiting but no worker running on this board
  [ "$ready" -gt 0 ] && [ "$running" -eq 0 ] && signals="${signals}${b}:IDLE(ready=$ready,running=0) "
  # CONDITION B — ORPHANED: ready/todo task with no assignee (dispatcher can't claim it)
  [ "$orphan" -gt 0 ] && signals="${signals}${b}:ORPHAN(unassigned=$orphan) "
  # CONDITION C — STALLED: pending work but nothing shipped in 24h
  [ "$ready" -gt 0 ] && [ "$done24" -eq 0 ] && signals="${signals}${b}:STALL(ready=$ready,done24h=0) "
  # CONDITION D — FAILING: a task crash-looping
  [ "$fails" -ge 3 ] && signals="${signals}${b}:FAILLOOP(maxfails=$fails) "
  # CONDITION E — BLOCK PILEUP
  [ "$blocked" -ge 5 ] && signals="${signals}${b}:BLOCKED(blocked=$blocked) "
done

if [ -n "$signals" ]; then
  sig=$(echo "$signals" | md5sum | cut -c1-12)
  if grep -qxF "$sig" "$STATE"; then
    echo "stall-watch: condition persists (Elon already woke): $signals"
    echo '{"wakeAgent": false}'
  else
    echo "$sig" >> "$STATE"
    echo "WAKE ELON — fleet condition(s): $signals"
    echo '{"wakeAgent": true}'
  fi
else
  : > "$STATE"
  echo "stall-watch: all 4 boards healthy (workers running, no orphans/stalls)"
  echo '{"wakeAgent": false}'
fi
