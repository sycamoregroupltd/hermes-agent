#!/usr/bin/env bash
# grok-work-cell.sh <board> <task_id> — ONE bounded grok pass on a kanban card.
# Contract (LITERAL): grok researches/drafts ONLY — no file writes, no state
# changes; the cell NEVER completes the card. Output is attached and the card
# ends blocked-for-review (needs_input) for an independent (non-grok) reviewer.
# Sign: fable-grok. Consumer: review lane completes/returns the card.
# Created 2026-08-02 (Frank directive: grok agents on todos, effectively+safely).
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
HK="${HK:-120}"   # bound every hermes call: unbounded calls starved the dispatcher (08-02)
BOARD="${1:?usage: grok-work-cell.sh <board> <task_id>}"
TID="${2:?usage: grok-work-cell.sh <board> <task_id>}"
OUTDIR=/home/frank/dgx-fable-orchestrator/state/grok-cells
mkdir -p "$OUTDIR"
OUT="$OUTDIR/$TID.md"

CTX=$(timeout "${HK:-120}" hermes kanban --board "$BOARD" context "$TID" 2>&1) || {
  echo "FATAL: could not fetch card context for $TID on $BOARD"; exit 1; }

# A card in `todo` CANNOT be blocked (hermes rejects: "not in running/ready"), so the
# review gate at the end would fail silently and the card would sit in todo with the
# draft attached but nothing marking it for review — where a hermes worker could pick
# it up and redo the work. Promote+claim first so the terminal block always lands.
# (Bug found 2026-08-02: 9 of 10 cells in one wave left their cards ungated.)
timeout "${HK:-120}" hermes kanban --board "$BOARD" promote "$TID" >/dev/null 2>&1 || true
timeout "${HK:-120}" hermes kanban --board "$BOARD" claim "$TID"   >/dev/null 2>&1 || true

BRIEF="You are a grok work-cell for Frank's DGX hermes fleet, executing ONE kanban card as a bounded research/drafting pass.
PARENT GOAL (one sentence): the Jarvis orchestrator is clearing the fleet's todo backlog by routing research-class cards to grok with independent Claude review before anything lands.
RULES (literal): produce your deliverable ENTIRELY as printed output (markdown). Do not write files. Do not run commands that change any state. Never claim the card is complete — an independent reviewer decides that. Mark anything you could not verify [UNVERIFIED]; an explicit 'could not verify' beats a plausible invention. Never touch: money, live trading, credentials, deploys, irreversible data operations, provider routing.
DELIVERABLE CONTRACT: (1) restate the card's objective in one line; (2) the work product itself, structured for direct reuse (research design/prereg/analysis as the card requires), citing sources/paths for every load-bearing claim; (3) an explicit ACCEPTANCE-TEST SELF-CHECK section quoting the card's acceptance criteria and honestly stating which parts your output does and does NOT satisfy; (4) open questions for the reviewer.
THE CARD (full context follows):
$CTX"

timeout --signal=TERM --kill-after=60 1800 grok -p "$BRIEF" > "$OUT" 2>&1
RC=$?
BYTES=$(wc -c < "$OUT")

if [ "$RC" -ne 0 ] || [ "$BYTES" -lt 400 ]; then
  timeout "${HK:-120}" hermes kanban --board "$BOARD" comment "$TID" --author fable-grok \
    "GROK WORK-CELL FAILED: rc=$RC bytes=$BYTES (budget 1800s). Output (may be partial): $OUTDIR/$TID.md. Card returned untouched — route to another tier."
  exit 1
fi

timeout "${HK:-120}" hermes kanban --board "$BOARD" comment "$TID" --author fable-grok \
  "GROK WORK-CELL OUTPUT ready for independent review (rc=$RC, ${BYTES}b, full text attached + at $OUTDIR/$TID.md). Head: $(head -c 600 "$OUT" | tr '\n' ' ')"
timeout "${HK:-120}" hermes kanban --board "$BOARD" attach "$TID" "$OUT" 2>/dev/null \
  || timeout "${HK:-120}" hermes kanban --board "$BOARD" comment "$TID" --author fable-grok "(attach unsupported/failed — full output at $OUTDIR/$TID.md)"
timeout "${HK:-120}" hermes kanban --board "$BOARD" block "$TID" "grok work-cell output attached - needs INDEPENDENT review (not grok, not the dispatching seat alone) before complete. Reviewer: verify against the card acceptance test, then complete with evidence or return with delta." --kind needs_input
echo "CELL DONE $BOARD/$TID rc=$RC bytes=$BYTES"
