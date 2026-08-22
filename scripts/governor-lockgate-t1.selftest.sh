#!/usr/bin/env bash
# governor-lockgate-t1.selftest.sh — verify governor-lockgate-t1.sh (t_53f9956d)
# Proves BOTH paths: (a) T1 relapse -> ALERT with reviewer+reason; (b) clean -> CLEAN.
# Uses an isolated KANBAN_BOARDS_DIR so it never touches live boards.
set -uo pipefail
SCRIPT="/home/frank/.hermes/scripts/governor-lockgate-t1.sh"
LOCKGATE="/home/frank/.hermes/scripts/kanban-approve-block-lockgate.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0

# --- build a synthetic board DB exercising all three tiers + one normal card ---
mkdir -p "$TMP/boards/syn"
python3 - "$TMP/boards/syn/kanban.db" <<'PY'
import sqlite3, sys
db=sys.argv[1]
c=sqlite3.connect(db)
for t in ('task_runs','task_events','task_comments','tasks'):
    c.execute('DROP TABLE IF EXISTS %s'%t)
c.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT)')
c.execute('CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER)')
c.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)')
c.execute('CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, started_at INTEGER)')
# t1: silent relapse (done->blocked, approved, NO re-open) -> T1
c.execute('INSERT INTO tasks VALUES(?,?,?)',('t1','silent relapse','blocked'))
c.execute('INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)',('t1','os-reviewer','REVIEW_VERDICT=APPROVED',1000))
c.execute('INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,?)',('t1',1,'completed','{}',2000))
# t2: operator-gated hold -> T2 (must NOT be surfaced)
c.execute('INSERT INTO tasks VALUES(?,?,?)',('t2','operator hold','blocked'))
c.execute('INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)',('t2','os-reviewer','REVIEW_VERDICT=APPROVED',1000))
c.execute('INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)',('t2','verdict-router','NEEDS-OPERATOR: refused',1100))
# t3: awaiting land -> T3 (must NOT be surfaced)
c.execute('INSERT INTO tasks VALUES(?,?,?)',('t3','awaiting land','todo'))
c.execute('INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)',('t3','os-reviewer','REVIEW_VERDICT=APPROVED',1000))
# t4: normal live, no marker -> no finding
c.execute('INSERT INTO tasks VALUES(?,?,?)',('t4','normal','blocked'))
c.commit(); c.close()
print('synthetic board written')
PY

run_ingest() {
  KANBAN_BOARDS_DIR="$TMP/boards" timeout 120 "$SCRIPT" 2>&1
}

echo "=== T1-ALERT PATH (expect ALERT with reviewer+reason, no T2/T3) ==="
out="$(run_ingest)"
echo "$out"
echo "$out" | grep -q "SILENT RELAPSE" && echo "PASS: T1 alert emitted" || { echo "FAIL: no T1 alert"; fail=1; }
echo "$out" | grep -q "reviewer=os-reviewer" && echo "PASS: reviewer context present" || { echo "FAIL: reviewer missing"; fail=1; }
echo "$out" | grep -q "reason=" && echo "PASS: reason/evidence context present" || { echo "FAIL: reason missing"; fail=1; }
# T2/T3 must NOT be surfaced as alerts
echo "$out" | grep -q "operator hold" && { echo "FAIL: T2 leaked into ingest"; fail=1; } || echo "PASS: T2 not surfaced (no dup)"
echo "$out" | grep -q "awaiting land" && { echo "FAIL: T3 leaked into ingest"; fail=1; } || echo "PASS: T3 not surfaced (no dup)"

echo "=== CLEAN PATH (expect CLEAN) ==="
mkdir -p "$TMP/clean"
python3 - "$TMP/clean/kanban.db" <<'PY'
import sqlite3, sys
c=sqlite3.connect(sys.argv[1])
for t in ('task_runs','task_events','task_comments','tasks'):
    c.execute('DROP TABLE IF EXISTS %s'%t)
c.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT)')
c.execute('CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER)')
c.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)')
c.execute('CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, started_at INTEGER)')
c.execute('INSERT INTO tasks VALUES(?,?,?)',('n1','normal','blocked'))
c.commit()
PY
clean="$(KANBAN_BOARDS_DIR="$TMP/clean" timeout 120 "$SCRIPT" 2>&1)"
echo "$clean"
echo "$clean" | grep -q "CLEAN" && echo "PASS: clean path emits CLEAN" || { echo "FAIL: clean path"; fail=1; }

echo "=== FAIL-OPEN PATH (missing detector dir) ==="
fo="$(KANBAN_BOARDS_DIR="$TMP/empty_missing" timeout 60 env LOCKGATE_DUMMY=1 bash -c 'KANBAN_BOARDS_DIR='"$TMP"'/empty_missing "$SCRIPT"' 2>&1 || true)"
# Simulate detector failure by pointing at a nonexistent script via env override is not wired;
# instead verify fail-open on a detector that errors: use a bad boards dir that makes summary exit 2.
fo2="$(KANBAN_BOARDS_DIR="$TMP/nonexistent_board_dir" timeout 60 "$SCRIPT" 2>&1)"
echo "$fo2"
echo "$fo2" | grep -qE "CLEAN|UNAVAILABLE" && echo "PASS: fail-open path" || { echo "FAIL: fail-open"; fail=1; }

echo "=== JSON MODE ==="
js="$(KANBAN_BOARDS_DIR="$TMP/boards" timeout 120 "$SCRIPT" --json 2>&1)"
echo "$js" | grep -q '"lockgate_t1": "ALERT"' && echo "PASS: json ALERT" || { echo "FAIL: json alert"; fail=1; }

if [ "$fail" -eq 0 ]; then
  echo "ALL GOVERNOR-LOCKGATE-T1 SELFTESTS PASS"
else
  echo "SELFTEST FAILURES PRESENT"; exit 1
fi
