#!/usr/bin/env bash
# Elon review-wake — wakeAgent SQL-gate. COMPLEMENTS elon-stall-watch (which catches
# STUCK/FAILING states). This catches POSITIVE flow needing oversight: work that
# COMPLETED and may need review-routing, or new dispatched work Elon should govern.
# md5-deduped so the same condition doesn't re-wake. Final line = {"wakeAgent":...}.
set -uo pipefail
BOARDS="upero jarvis-os sycode-ai sycode-trading"
STATE="/home/frank/.hermes/cron/state/elon-review-wake.seen"
mkdir -p "$(dirname "$STATE")"; touch "$STATE"

signals=""
for b in $BOARDS; do
  db="/home/frank/.hermes/kanban/boards/$b/kanban.db"
  [ -f "$db" ] || continue
  # tasks completed in the last 30 min (fresh output that may need review/follow-up routing)
  done30=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status='done' AND completed_at > strftime('%s','now','-30 minutes')" 2>/dev/null || echo 0)
  # tasks newly running (active work Elon should be aware of for cross-project prioritization)
  running=$(sqlite3 "$db" "SELECT COUNT(*) FROM task_runs WHERE status='running'" 2>/dev/null || echo 0)
  # review-required tasks sitting unreviewed (a 'review' card that hasn't been actioned)
  reviewq=$(sqlite3 "$db" "SELECT COUNT(*) FROM tasks WHERE status IN ('ready','todo') AND (title LIKE 'REVIEW%' OR title LIKE '%review%') " 2>/dev/null || echo 0)
  done30=${done30:-0}; running=${running:-0}; reviewq=${reviewq:-0}

  # CONDITION R1 — fresh completions needing oversight/follow-up routing
  [ "$done30" -ge 2 ] && signals="${signals}${b}:REVIEW(done30m=$done30) "
  # CONDITION R2 — review-card backlog (work explicitly awaiting review, not being picked up)
  [ "$reviewq" -ge 1 ] && signals="${signals}${b}:REVIEWQ(awaiting=$reviewq) "
done

if [ -n "$signals" ]; then
  sig=$(echo "$signals" | md5sum | cut -c1-12)
  if grep -qxF "$sig" "$STATE"; then
    echo "review-wake: condition persists (Elon already aware): $signals"
    echo '{"wakeAgent": false}'
  else
    echo "$sig" >> "$STATE"
    echo "WAKE ELON (review/oversight) — $signals : assess completed work, route any needing review (guardian/linked card), reprioritize cross-project. Do NOT dispatch (dispatcher owns that) — govern + route."
    echo '{"wakeAgent": true}'
  fi
else
  : > "$STATE"
  echo '{"wakeAgent": false}'
fi
exit 0
