#!/usr/bin/env bash
# grok-cell-wave.sh <parallelism> <board:task_id> [board:task_id ...]
# Runs grok work-cells over a card list with bounded parallelism.
# Each cell inherits grok-work-cell.sh's contract: draft/research only,
# output attached, card ends blocked-for-review, NEVER self-completed.
# Consumer: review lane. Created 2026-08-02 (Frank: maximize grok inference).
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
P="${1:?usage: grok-cell-wave.sh <parallelism> <board:task> ...}"; shift
CELL=/home/frank/.hermes/scripts/grok-work-cell.sh
i=0
for spec in "$@"; do
  b="${spec%%:*}"; t="${spec##*:}"
  bash "$CELL" "$b" "$t" &
  i=$((i+1))
  [ $(( i % P )) -eq 0 ] && wait
done
wait
echo "WAVE DONE: $# cells dispatched (outputs in dgx-fable-orchestrator/state/grok-cells/, cards blocked-for-review)"
