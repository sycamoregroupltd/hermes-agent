#!/usr/bin/env bash
set -euo pipefail

BOARD="jarvis-os"
HERMES="/home/frank/.local/bin/hermes"
DB="/home/frank/.hermes/kanban/boards/${BOARD}/kanban.db"
STATE_DIR="/home/frank/.hermes/state"
STATE_FILE="${STATE_DIR}/mission-control-monitor.snapshot"
DONE_FILE="${STATE_DIR}/mission-control-monitor.done-reported"
mkdir -p "$STATE_DIR"

IDS=(
  t_5bee7482
  t_13a78bc0
  t_d7517382
  t_dd2002e4
  t_393949fc
  t_d2a3e42a
  t_3a09ff48
  t_374458a6
  t_a6ff27e7
  t_e3f3f014
  t_398c403e
  t_0e952605
  t_7ab20ab9
)

# Keep the board moving. Do not fail the monitor if a dispatch pass has no spawn or transient CLI issue.
DISPATCH_OUT="$($HERMES kanban --board "$BOARD" dispatch 2>&1 || true)"

TMP="$(mktemp)"
python3 - "$DB" "${IDS[@]}" > "$TMP" <<'PY'
import sqlite3, sys, json, time
path=sys.argv[1]; ids=sys.argv[2:]
con=sqlite3.connect(path); con.row_factory=sqlite3.Row
rows=[]
for tid in ids:
    r=con.execute('select id,title,status,assignee,started_at,completed_at from tasks where id=?',(tid,)).fetchone()
    if r: rows.append(dict(r))
counts={}
for r in rows: counts[r['status']]=counts.get(r['status'],0)+1
print(json.dumps({'ts': int(time.time()), 'counts': counts, 'rows': rows}, sort_keys=True))
PY

SNAPSHOT="$(cat "$TMP")"
PREV=""
if [ -f "$STATE_FILE" ]; then PREV="$(cat "$STATE_FILE")"; fi
printf '%s\n' "$SNAPSHOT" > "$STATE_FILE"

# Emit only when there is a status snapshot change, a blocker, a fresh dispatch spawn, or all tasks finish.
CHANGED=0
[ "$SNAPSHOT" != "$PREV" ] && CHANGED=1
SPAWNED="$(printf '%s\n' "$DISPATCH_OUT" | awk '/Spawned:/ {print $2+0; found=1} END{if(!found) print 0}')"
BLOCKED="$(python3 - "$TMP" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for r in d['rows'] if r['status']=='blocked'))
PY
)"
ALL_DONE="$(python3 - "$TMP" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
rows=d['rows']
print('1' if rows and all(r['status']=='done' for r in rows) else '0')
PY
)"

if [ "$ALL_DONE" = "1" ]; then
  if [ ! -f "$DONE_FILE" ]; then
    touch "$DONE_FILE"
    echo "DGX Mission Control improvements: ALL TRACKED CARDS DONE."
  else
    rm -f "$TMP"
    exit 0
  fi
elif [ "$CHANGED" = "1" ] || [ "${SPAWNED:-0}" -gt 0 ] || [ "${BLOCKED:-0}" -gt 0 ]; then
  python3 - "$TMP" "$SPAWNED" <<'PY'
import json, sys, time
d=json.load(open(sys.argv[1])); spawned=sys.argv[2]
print('DGX Mission Control monitor update')
print('time_utc:', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(d['ts'])))
print('dispatch_spawned:', spawned)
print('counts:', ', '.join(f"{k}={v}" for k,v in sorted(d['counts'].items())))
for r in d['rows']:
    if r['status'] in ('running','blocked','ready'):
        print(f"- {r['id']} {r['status']} {r['assignee']}: {r['title']}")
PY
fi
rm -f "$TMP"
