#!/usr/bin/env bash
# grok-seat-tick.sh — persist the grok TERMINAL-LANE seat without a Hermes profile.
#
# Hermes agents are "always available" because the dispatcher can spawn them.
# grok must NEVER live under ~/.hermes/profiles/ (that re-enables auto-spawn
# with the wrong model). This tick is the seat's dispatcher analog:
#   ready + assignee=grok → claim → grok-4.6 headless with a durable session
#   → the model completes or blocks, signed grok.
#
# Skip Frank/outage/dedupe parks. Empty stdout = silent. Any stdout is an alert.
# Cron: no-agent, every 5m. Created 2026-08-13 (Frank: persist session seats).
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
STATE=/home/frank/dgx-fable-orchestrator/state
LOG=$STATE/grok-seat.log
SEAT=/home/frank/.hermes/seats/grok
SID_FILE=$SEAT/persistent-session.id
SOUL=$SEAT/SOUL.md
BOARDS="jarvis-os sycode-trading upero yorkstone-supplies orchestrator-sync"
MAX_MS=900000

mkdir -p "$STATE" "$SEAT"
exec 9>"$STATE/grok-seat.lock"
flock -n 9 || exit 0

command -v grok >/dev/null || { echo "GROK SEAT BROKEN: grok CLI missing"; exit 0; }
# Max-plan OIDC only (~/.grok/auth.json). Never bill the Grok API key path.
unset XAI_API_KEY
[[ -f "$SOUL" ]] || { echo "GROK SEAT BROKEN: $SOUL missing"; exit 0; }

is_park() {
  local board="$1" tid="$2"
  local db="$HOME/.hermes/kanban/boards/$board/kanban.db"
  sqlite3 "file:${db}?mode=ro" "SELECT body FROM task_comments WHERE task_id='$tid' ORDER BY id DESC LIMIT 8;" 2>/dev/null \
    | rg -q 'FRANK-DEPLOY-PARK|FRANK-GATE-PARK|OUTAGE-PARK|dedupe-park|coordinate-park|DUPLICATE of running'
}

pick() {
  local board db
  # Prefer Buzz-reply cards (the relay wire), then other ready grok work.
  for prefer in buzz other; do
    for board in $BOARDS; do
      db="$HOME/.hermes/kanban/boards/$board/kanban.db"
      [[ -f "$db" ]] || continue
      local sql="SELECT id FROM tasks WHERE status='ready' AND assignee='grok'"
      if [[ "$prefer" == buzz ]]; then
        sql="$sql AND title LIKE 'BUZZ REPLY:%'"
      else
        sql="$sql AND title NOT LIKE 'BUZZ REPLY:%'"
      fi
      sql="$sql ORDER BY created_at ASC LIMIT 5;"
      local ids
      ids=$(sqlite3 "file:${db}?mode=ro" "$sql" 2>/dev/null || true)
      local tid
      for tid in $ids; do
        [[ -n "$tid" ]] || continue
        is_park "$board" "$tid" && continue
        echo "$board $tid"
        return 0
      done
    done
  done
  return 1
}

picked=$(pick) || exit 0
board=${picked%% *}
tid=${picked##* }

timeout 60 hermes kanban --board "$board" comment "$tid" --author grok \
  "SEAT-TICK: grok persistent seat claiming this ready card (session file $SID_FILE). Driver is Grok Build TUI / grok-4.6, not a Hermes profile." >/dev/null 2>&1 || true
timeout 60 hermes kanban --board "$board" claim "$tid" >/dev/null 2>&1 || true

CTX=$(timeout 60 hermes kanban --board "$board" context "$tid" 2>&1) || CTX="(context fetch failed)"

BRIEF="You are the grok terminal-lane seat. Read $SOUL first — it is your contract.
Identity: grok. NEVER create or inhabit ~/.hermes/profiles/grok.
Primary fleet model for Hermes workers is nous deepseek/deepseek-v4-flash-0731. YOU are grok-4.6.
Board $board task $tid. Sign comments --author grok. End with hermes kanban complete or block.

Hard gates: paper-only; no live trading; no credential changes; no prod deploy/DDL unless Frank named this exact operation; no force-push; no push to main.

THE CARD:
$CTX"

# Durable session so this seat accumulates memory like a long-lived Hermes gateway.
GROK_ARGS=(--always-approve --model grok-4.6 --reasoning-effort xhigh --output-format plain)
if [[ -s "$SID_FILE" ]]; then
  GROK_ARGS+=(-r "$(tr -d '[:space:]' < "$SID_FILE")")
else
  NEW_SID=$(python3 -c 'import uuid; print(uuid.uuid4())')
  printf '%s\n' "$NEW_SID" > "$SID_FILE"
  GROK_ARGS+=(-s "$NEW_SID")
fi

timeout --signal=TERM --kill-after=30 $((MAX_MS/1000)) grok "${GROK_ARGS[@]}" -p "$BRIEF" \
  >> "$LOG" 2>&1
rc=$?
echo "$(date -u +%FT%TZ) tick board=$board tid=$tid grok_rc=$rc sid=$(tr -d '[:space:]' < "$SID_FILE" 2>/dev/null)" >> "$LOG"
# If resume failed because the session vanished, drop the id so the next tick creates one.
if [[ "$rc" -ne 0 ]] && rg -q 'session|not found|does not exist' "$LOG"; then
  rm -f "$SID_FILE"
fi
exit 0
