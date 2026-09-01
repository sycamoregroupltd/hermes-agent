#!/usr/bin/env bash
# fleet-converge-tick.sh — one convergence pass toward "clean and up to date".
#
# Goal (Frank, 2026-08-30): loop until every board is clean, not just churn.
# A dispatch-only loop CANNOT converge while intake >= drain, so this tick does
# three things in order, cheapest first:
#
#   1. RECLAIM  — dispatch pass per active board (fills free worker slots).
#   2. RECOVER  — unblock crash-blocked cards that have PROOF of completed work
#                 (verify-not-redo; classifier only acts on REVIEWER_CRASH /
#                 CONTEXT_EXHAUST, never NO_EVIDENCE).
#   3. REPORT   — print a line ONLY when something changed (watchdog pattern:
#                 empty stdout => cron delivers nothing).
#
# Deliberately NOT done here (needs Frank / A3):
#   - raising kanban.max_in_progress
#   - --resolve-stale-refs (would falsely COMPLETE live PR-review cards)
#   - killing live workers
#
# Boards come from boards-manifest.json (state=active AND dispatch=true), never
# a hardcoded list and never `--boards all` (that globs test dirs and the
# denied orchestrator-sync board).
set -uo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
unset HERMES_DELEGATED_CHILD_CONTEXT 2>/dev/null || true
STATE=/home/frank/.hermes/cron/state
mkdir -p "$STATE"
LOCK="$STATE/fleet-converge.lock"
exec 9>"$LOCK"
flock -n 9 || exit 0   # a previous tick is still running; skip silently

MANIFEST=/home/frank/.hermes/kanban/boards-manifest.json
BOARDS=$(python3 -c "
import json
d=json.load(open('$MANIFEST'))['boards']
print(' '.join(sorted(k for k,v in d.items() if v.get('state')=='active' and v.get('dispatch'))))
" 2>/dev/null)
[ -n "$BOARDS" ] || exit 0

changed=0
lines=""

# ---- 1. dispatch ---------------------------------------------------------
for b in $BOARDS; do
  out=$(timeout 300 hermes kanban --board "$b" dispatch --json 2>/dev/null) || continue
  n=$(printf '%s' "$out" | python3 -c "
import json,sys
try: print(len(json.load(sys.stdin).get('spawned') or []))
except Exception: print(0)" 2>/dev/null || echo 0)
  if [ "${n:-0}" -gt 0 ]; then
    lines="${lines}  dispatch ${b}: spawned ${n}\n"
    changed=$((changed+n))
  fi
done

# ---- 2. recover crash-blocked cards with evidence ------------------------
TRIAGE=/home/frank/.hermes/scripts/triage_crash_blocked.py
if [ -f "$TRIAGE" ]; then
  rec=$(TRIAGE_LIMIT=8 timeout 600 python3 "$TRIAGE" --apply 2>/dev/null | grep -c '^UNBLOCKED' || true)
  if [ "${rec:-0}" -gt 0 ]; then
    lines="${lines}  recovered ${rec} crash-blocked card(s) (verify-not-redo)\n"
    changed=$((changed+rec))
  fi
fi

# ---- 3. report only on change -------------------------------------------
if [ "$changed" -gt 0 ]; then
  q=$(python3 -c "
import sqlite3,json
d=json.load(open('$MANIFEST'))['boards']
tq=tb=0
for b,v in d.items():
    if not(v.get('state')=='active' and v.get('dispatch')): continue
    try: c=sqlite3.connect(f'/home/frank/.hermes/kanban/boards/{b}/kanban.db')
    except Exception: continue
    tq+=c.execute(\"SELECT count(*) FROM tasks WHERE status IN ('ready','todo')\").fetchone()[0]
    tb+=c.execute(\"SELECT count(*) FROM tasks WHERE status='blocked'\").fetchone()[0]
    c.close()
print(f'{tq} queued / {tb} blocked')
" 2>/dev/null)
  printf 'fleet-converge: %d action(s)\n' "$changed"
  printf '%b' "$lines"
  [ -n "$q" ] && printf '  fleet now: %s\n' "$q"
fi
exit 0
