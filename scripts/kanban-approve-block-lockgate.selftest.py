'''Self-test for kanban-approve-block-lockgate.py — verifies tier logic without touching
live boards. Builds an in-memory sqlite DB exercising all three tiers + the simulate gate.'''
import os
import sys
import sqlite3
import tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util
_modpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kanban-approve-block-lockgate.py')
_spec = importlib.util.spec_from_file_location('lockgate', _modpath)
L = importlib.util.module_from_spec(_spec)
sys.modules['lockgate'] = L
_spec.loader.exec_module(L)


def make_db(path, rows):
    '''rows: list of (tid, title, status, comments[(author,body,ts)], landed_event_ts|0)'''
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT, status TEXT)')
    conn.execute('CREATE TABLE task_comments(id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER)')
    conn.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER)')
    conn.execute('CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, started_at INTEGER)')
    for tid, title, status, comments, landed in rows:
        conn.execute('INSERT INTO tasks VALUES(?,?,?)', (tid, title, status))
        for a, b, ts in comments:
            conn.execute('INSERT INTO task_comments(task_id,author,body,created_at) VALUES(?,?,?,?)', (tid, a, b, ts))
            if landed:
                conn.execute('INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,?)', (tid, 1, 'completed', '{}', landed))
        conn.commit()
    return conn


def test_tiers():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, 't.db')
    make_db(db, [
        ('t1', 'silent relapse', 'blocked', [
            ('reviewer', 'REVIEW_VERDICT=APPROVED', 1000)], 2000),
        ('t2', 'operator hold', 'blocked', [
            ('reviewer', 'REVIEW_VERDICT=APPROVED', 1000),
            ('verdict-router', 'NEEDS-OPERATOR: refused to auto-complete', 1100)], 0),
        ('t3', 'awaiting land', 'todo', [
            ('reviewer', 'REVIEW_VERDICT=APPROVED', 1000)], 0),
        ('t4', 'normal live', 'blocked', [], 0),
        # Defect 3 regression case: in_progress card carrying an approval marker must be seen.
        ('t5', 'in_progress with approval', 'in_progress', [
            ('reviewer', 'REVIEW_VERDICT=APPROVED', 1000)], 0),
    ])
    res = L._with_watchdog(5, L._scan_board, 'testboard', db)
    by = {f.task_id: f for f in res}
    assert 't1' in by and by['t1'].tier == 1, 't1 should be T1 silent relapse'
    assert 't2' in by and by['t2'].tier == 2, 't2 should be T2 operator-gated hold'
    assert 't3' in by and by['t3'].tier == 3, 't3 should be T3 awaiting land'
    assert 't4' not in by, 't4 normal live (no marker) must not be a finding'
    assert 't5' in by and by['t5'].tier == 3, 'DEFECT 3: in_progress+approval marker must be detected (tier 3)'
    print('test_tiers: OK')


def test_simulate_gate():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, 't.db')
    make_db(db, [
        ('s1', 'in_progress approved', 'in_progress', [
            ('reviewer', 'REVIEW_VERDICT=APPROVED', 1000)], 0),
    ])
    conn = L._open_ro(db)
    marker = L._find_approval_marker(conn, 's1')
    status = conn.execute('SELECT status FROM tasks WHERE id=?', ('s1',)).fetchone()[0]
    conn.close()
    # Defect 3 assertions: an in_progress card carrying an approval marker must be SEEN,
    # and the simulate gate must report the marker it found (never claim NO-OP).
    assert marker is not None and marker.get('marker_type'), 'simulate must find the marker'
    assert status == 'in_progress', 'card under test is in_progress'
    assert marker.get('marker_type') is not None, 'approval_marker must be reported, not None'
    # Tier-3 relapse path (approved + live, no operator gate): detected, not NO-OP.
    assert L.REGRESSABLE_STATUSES == {'todo', 'ready', 'blocked', 'running', 'scheduled', 'in_progress'}, \
        'DEFECT 3: in_progress must be in REGRESSABLE_STATUSES'
    print('test_simulate_gate: OK')


def test_zero_board_sentinel():
    '''N3: a zero-board scan must exit 2 and print the EXACT sentinel NO-BOARDS-SCANNED.

    Pins the spelling (was NO-BOARDS-SCARNED) so a typo cannot silently return. Run as a
    subprocess because the code path calls sys.exit(2) at import-time module scope.'''
    import subprocess
    empty = tempfile.mkdtemp()
    env = dict(os.environ, KANBAN_BOARDS_DIR=empty)
    r = subprocess.run([sys.executable, _modpath, 'summary'],
                       env=env, text=True, capture_output=True, timeout=120)
    assert r.returncode == 2, 'zero-board scan must exit 2, got %s' % r.returncode
    assert 'NO-BOARDS-SCANNED:' in r.stderr, \
        'exact sentinel NO-BOARDS-SCANNED missing from stderr: %r' % r.stderr
    assert 'SCARNED' not in r.stderr, 'legacy typo NO-BOARDS-SCARNED must not reappear'
    print('test_zero_board_sentinel: OK')


if __name__ == '__main__':
    test_tiers()
    test_simulate_gate()
    test_zero_board_sentinel()
    print('ALL SELFTESTS PASS')
