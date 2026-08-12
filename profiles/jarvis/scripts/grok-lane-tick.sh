#!/usr/bin/env bash
# grok-lane-tick.sh — CONTINUOUS grok work lane (no-agent cron job).
# Picks grok-safe cards off the boards, runs bounded-parallel work cells, and
# leaves every output blocked-for-review. Never completes a card.
# Created 2026-08-02 (Frank: "do we have multiple grok seats running" — we did
# not; manual waves were a seat-dependency, i.e. a gap. This is the mechanism.)
#
# DESIGN RULES (literal):
#  - BACKPRESSURE FIRST: if too many cards already await grok review, do
#    nothing. Drafting faster than we review just builds a new black hole.
#  - Cards are picked ONLY from safe classes (research/doc/analysis/spec) and
#    must NOT match A3-adjacent words (deploy/credential/live/migrate/DDL...).
#  - Cells never self-complete: grok-work-cell.sh blocks each card
#    needs_input for an INDEPENDENT (non-grok) reviewer.
#  - Empty stdout = silent (fleet no-agent watchdog pattern); any output is
#    delivered as an alert.
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
CELLS=/home/frank/.hermes/scripts/grok-work-cell.sh
WAVE=/home/frank/.hermes/scripts/grok-cell-wave.sh
STATE=/home/frank/dgx-fable-orchestrator/state
LOG=$STATE/grok-lane.log
BOARDS="sycode-trading jarvis-os upero ai-restaurant"
# CAPACITY (measured 2026-08-02, not guessed): 10 concurrent `grok -p` sessions
# all returned correct answers in 25-35s with ZERO rate-limit errors. 10 seats
# is therefore proven safe. The binding constraint is NOT grok — it is REVIEW
# capacity: every draft needs an independent (non-grok) reviewer before it can
# land, so MAX_IN_REVIEW is the real governor. Raise it only when review
# throughput is proven to keep up.
MAX_IN_REVIEW=20     # backpressure ceiling: awaiting-grok-review cards fleet-wide
BATCH=10             # cards per tick
PAR=10               # concurrent cells (proven ceiling)

mkdir -p "$STATE"
exec 9>"$STATE/grok-lane.lock"
flock -n 9 || exit 0   # a tick is already running

[ -x "$CELLS" ] || { echo "GROK LANE BROKEN: $CELLS missing/not executable"; exit 0; }

# --- backpressure: count cards already carrying a grok cell output awaiting review
inreview=0
for b in $BOARDS; do
  db="$HOME/.hermes/kanban/boards/$b/kanban.db"
  [ -f "$db" ] || continue
  n=$(sqlite3 "file:${db}?mode=ro" "SELECT COUNT(DISTINCT c.task_id) FROM task_comments c
      JOIN tasks t ON t.id=c.task_id
      WHERE t.status='blocked' AND c.author='fable-grok'
        AND c.body LIKE '%ready for independent review%';" 2>/dev/null || echo 0)
  inreview=$((inreview + n))
done
if [ "$inreview" -ge "$MAX_IN_REVIEW" ]; then
  echo "$(date -u +%FT%TZ) BACKPRESSURE: $inreview cards await grok review (max $MAX_IN_REVIEW) — skipping tick" >> "$LOG"
  exit 0   # silent: this is healthy throttling, not a fault
fi

# --- pick grok-safe candidates: ready/todo, safe class, no A3-adjacent words
picks=""
want=$BATCH
for b in $BOARDS; do
  [ "$want" -gt 0 ] || break
  db="$HOME/.hermes/kanban/boards/$b/kanban.db"
  [ -f "$db" ] || continue
  ids=$(sqlite3 "file:${db}?mode=ro" "
    SELECT id FROM tasks
    WHERE status = 'ready'          -- READY ONLY. A 'todo' card may be dependency-gated
                                    -- (unsatisfied parents); drafting against it produces work
                                    -- whose prerequisites are not done, and the card cannot even
                                    -- be gated for review. Bug found 2026-08-02: 9 of 10 cells in
                                    -- one wave hit dependency-gated todo cards.
      -- WIDENED 2026-08-03: the research/docs-only filter matched 1 of 30 ready cards, so the
      -- ONE working inference tier sat idle while implementation work waited. grok now also takes
      -- IMPLEMENTATION-DRAFT work (fix/implement/harden/triage/repair). It still never lands
      -- anything: the cell attaches a draft and blocks the card for an independent reviewer, and
      -- MAX_IN_REVIEW stops the lane if drafts pile up unreviewed.
      AND (lower(title) LIKE '%research%' OR lower(title) LIKE '%document%'
        OR lower(title) LIKE '%analy%'   OR lower(title) LIKE '%spec%'
        OR lower(title) LIKE '%catalog%' OR lower(title) LIKE '%audit%'
        OR lower(title) LIKE '%study%'   OR lower(title) LIKE '%diagnos%'
        OR lower(title) LIKE '%classif%' OR lower(title) LIKE '%synthes%'
        OR lower(title) LIKE '%fix %'    OR lower(title) LIKE '%implement%'
        OR lower(title) LIKE '%harden%'  OR lower(title) LIKE '%triage%'
        OR lower(title) LIKE '%repair%'  OR lower(title) LIKE '%gate %')
      AND lower(title) NOT LIKE '%deploy%'   AND lower(title) NOT LIKE '%credential%'
      AND lower(title) NOT LIKE '%secret%'   AND lower(title) NOT LIKE '%live trading%'
      AND lower(title) NOT LIKE '%migrat%'   AND lower(title) NOT LIKE '%ddl%'
      AND lower(title) NOT LIKE '%merge%'    AND lower(title) NOT LIKE '%restart%'
      AND lower(title) NOT LIKE '%frank%'    AND lower(title) NOT LIKE '%a3%'
      AND NOT EXISTS (SELECT 1 FROM task_comments c
                      WHERE c.task_id=tasks.id AND c.author='fable-grok')
    ORDER BY priority LIMIT $want;" 2>/dev/null)
  for id in $ids; do
    picks="$picks $b:$id"
    want=$((want-1))
  done
done

# shellcheck disable=SC2086
set -- $picks
if [ "$#" -eq 0 ]; then
  echo "$(date -u +%FT%TZ) no grok-safe candidates this tick (in_review=$inreview)" >> "$LOG"
  exit 0   # silent: an honest empty queue is not a fault
fi

echo "$(date -u +%FT%TZ) dispatching $# cells (in_review=$inreview): $*" >> "$LOG"
bash "$WAVE" "$PAR" "$@" >> "$LOG" 2>&1
echo "$(date -u +%FT%TZ) tick done" >> "$LOG"
exit 0   # silent on success; failures are recorded per-card by the cell itself
