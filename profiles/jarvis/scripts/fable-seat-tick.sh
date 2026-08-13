#!/usr/bin/env bash
# fable-seat-tick.sh — persist the fable (Claude Code) terminal-lane seat.
# Same contract as grok-seat-tick.sh. NEVER create ~/.hermes/profiles/fable.
# Uses `claude -p`. Skip Frank/outage/dedupe parks. Empty stdout = silent.
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
STATE=/home/frank/dgx-fable-orchestrator/state
LOG=$STATE/fable-seat.log
SEAT=/home/frank/.hermes/seats/fable
SOUL=$SEAT/SOUL.md
BOARDS="jarvis-os sycode-trading upero yorkstone-supplies orchestrator-sync"

mkdir -p "$STATE"
exec 9>"$STATE/fable-seat.lock"
flock -n 9 || exit 0

command -v claude >/dev/null || exit 0
[[ -f "$SOUL" ]] || exit 0

is_park() {
  local board="$1" tid="$2" db="$HOME/.hermes/kanban/boards/$board/kanban.db"
  sqlite3 "file:${db}?mode=ro" "SELECT body FROM task_comments WHERE task_id='$tid' ORDER BY id DESC LIMIT 8;" 2>/dev/null \
    | rg -q 'FRANK-DEPLOY-PARK|FRANK-GATE-PARK|OUTAGE-PARK|dedupe-park|coordinate-park|DUPLICATE of running'
}

picked=""
for board in $BOARDS; do
  db="$HOME/.hermes/kanban/boards/$board/kanban.db"
  [[ -f "$db" ]] || continue
  ids=$(sqlite3 "file:${db}?mode=ro" "SELECT id FROM tasks WHERE status='ready' AND assignee='fable' ORDER BY created_at ASC LIMIT 5;" 2>/dev/null || true)
  for tid in $ids; do
    [[ -n "$tid" ]] || continue
    is_park "$board" "$tid" && continue
    picked="$board $tid"
    break
  done
  [[ -n "$picked" ]] && break
done
[[ -n "$picked" ]] || exit 0
board=${picked%% *}; tid=${picked##* }

timeout 60 hermes kanban --board "$board" comment "$tid" --author fable \
  "SEAT-TICK: fable persistent seat claiming this ready card. Claude Code terminal lane, not a Hermes profile." >/dev/null 2>&1 || true
timeout 60 hermes kanban --board "$board" claim "$tid" >/dev/null 2>&1 || true
CTX=$(timeout 60 hermes kanban --board "$board" context "$tid" 2>&1) || CTX="(context fetch failed)"

timeout --signal=TERM --kill-after=30 900 claude -p --permission-mode bypassPermissions \
  "You are the fable terminal-lane seat. Read $SOUL first. NEVER create ~/.hermes/profiles/fable.
Board $board task $tid. Sign comments --author fable. End with hermes kanban complete or block.
Hard gates: paper-only; no live trading; no credential changes; no prod deploy/DDL unless Frank named this operation; no force-push.
THE CARD:
$CTX" >> "$LOG" 2>&1
echo "$(date -u +%FT%TZ) tick board=$board tid=$tid claude_rc=$?" >> "$LOG"
exit 0
