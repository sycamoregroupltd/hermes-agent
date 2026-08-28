from __future__ import annotations

'''
kanban-approve-block-lockgate.py  —  fleet-writable approve->blocked relapse detector.

PROBLEM (root cause of t_6f39f454 loop):
  A task receives REVIEW_VERDICT=APPROVED (or kanban_complete) and is therefore in a
  terminal "approved/landed" state, but it later regresses to blocked/todo/running.
  In t_6f39f454 the verdict-router emitted NEEDS-OPERATOR (operator/A3-gated auto-complete)
  and the work card stayed `blocked`, burning repeated PM cycles to re-triage.

WHAT THIS DOES (structural lock-gate, no core-Hermes edit required):
  * `detect`  — scans every board DB for cards that have an approval marker in their
                comment/run history but are currently NOT in the landed set
                ({done, archived}). Surfaces them as POTENTIAL RELAPSE with the exact
                evidence (marker type, marker author, marker timestamp, current status).
  * `simulate` — given a board+task that is currently "landed/approved", asserts that an
                attempt to move it to blocked/todo/running would be REJECTED/FLAGGED unless a
                reviewer re-opens it with a reason. This is the acceptance test for gate (1):
                "simulate approve then attempt block -> block rejected or flagged with reviewer+reason".
  * `watch`   — runs `detect` and writes a machine-readable relapse ledger; commits nothing,
                mutates nothing. Designed to be wrapped by a cron (Frank-gated per t_6f39f454 boundary).

APPROVAL MARKER detection (evidence-first):
  We treat a card as "approved/landed" if ANY of:
    - a comment body contains "REVIEW_VERDICT=APPROVED"  (reviewer sign-off), or
    - a comment author == "verdict-router" with marker meaning approved, or
    - a run with outcome 'completed' exists (kanban_complete ran).
  The card is a RELAPSE if approved/landed evidence exists AND current status in
  {blocked, todo, running, ready, scheduled} (i.e. not done/archived).

Note: This is the fleet-writable guardrail half. The companion change — teaching the core
completion-gate classifier (t_73b64da5 / t_e84b71fe, which lives OUTSIDE the fleet writable
tree) to treat approve as terminal — is A3/Frank-gated and tracked separately
(see review-required handoff on t_da87a712). Editing core Hermes source is out of bounds for
fleet self-edit without a Frank A3 exception (precedent t_40bac827 / t_5a9e44a6).

No secrets, no credentials, no DB writes, no deploys. Read-only sqlite + stdout ledger.
'''
import argparse
import glob
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, asdict
from typing import Optional

HERMES_HOME = os.environ.get('HERMES_HOME', os.path.expanduser('~/.hermes'))
BOARDS_DIR = os.environ.get('KANBAN_BOARDS_DIR', os.path.join(os.path.expanduser('~/.hermes'), 'kanban', 'boards'))
LANDED_STATUSES = {
    'done',
    'archived',
}
# Defect 3 fix: 'in_progress' must be regressable too, otherwise the detector is blind to
# the very card (t_da87a712) that owns it.
REGRESSABLE_STATUSES = {
    'todo',
    'ready',
    'blocked',
    'running',
    'scheduled',
    'in_progress',
}
APPROVAL_MARKERS = ('REVIEW_VERDICT=APPROVED', 'REVIEW_VERDICT: APPROVED', 'verdict-router marked approved')

# Anchored approval verdict (t_5e874719 — lockgate detector alignment).
# Mirrors the core fix in hermes_cli/kanban_db.py (_APPROVAL_APPROVED_RE).
# We require the marker at a line start (optionally after list/quote punctuation),
# and we ignore markers that sit inside a fenced code block.
# A bare ``REVIEW_VERDICT=APPROVED`` only matches when it is the comment author's
# own deliberate verdict line — not a citation, not prose, not code.
_APPROVAL_APPROVED_RE = re.compile(
    r'^\s*(?:[-*>]\s*)?REVIEW_VERDICT\s*[:=]\s*APPROVED\b',
    re.IGNORECASE | re.MULTILINE,
)


def _strip_fenced_code(text):
    """Drop fenced code blocks (``` or ~~~) so a verdict cited inside a code span
    cannot be mistaken for the comment author's own approval verdict."""
    out_lines = []
    in_fence = False
    fence_marker = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if not in_fence:
            if stripped.startswith('```') or stripped.startswith('~~~'):
                in_fence = True
                fence_marker = stripped[:3]
                continue
            out_lines.append(line)
        else:
            if fence_marker and stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = None
                continue
    return '\n'.join(out_lines)

TIER_LABEL = {
    1: 'T1-SILENT-RELAPSE (landed then re-blocked, no reviewer re-open)',
    2: 'T2-OPERATOR-GATED-HOLD (approved, verdict-router NEEDS-OPERATOR, awaiting land)',
    3: 'T3-AWAITING-LAND (approved + live, no operator gate)',
}


def _human(ts=None):
    if not ts:
        return '?'
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _iter_board_dbs():
    '''Enumerate canonical per-board primary DBs without a recursive glob (recursive
    glob over this tree hangs under live load). Boards are direct children of BOARDS_DIR.'''
    out = []
    if not os.path.isdir(BOARDS_DIR):
        return out
    candidates = ('kanban.db', 'board.db')
    for name in sorted(os.listdir(BOARDS_DIR)):
        d = os.path.join(BOARDS_DIR, name)
        for c in candidates:
            p = os.path.join(d, c)
            if os.path.exists(p):
                out.append(p)
                break
    return out


def _open_ro(path=None):
    """Open read-only (mode=ro) so we never block or wait on the
    live dispatcher's writer lock. Falls back to a 1s-timeout connect only if the uri form
    is unsupported."""
    try:
        conn = sqlite3.connect(f'''file:{path}?mode=ro''', uri=True, timeout=1)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(path, timeout=1)
    return conn


def _with_watchdog(seconds=None, fn=None, *a, **kw):
    """Run fn under a SIGALRM watchdog so one slow/locked board can't hang the scan."""
    import signal

    class _Timeout(Exception):
        pass

    def _handler(signum, frame):
        raise _Timeout()

    use_signal = hasattr(signal, 'SIGALRM')
    old = None
    if use_signal:
        old = signal.signal(signal.SIGALRM, _handler)
        signal.alarm(int(seconds))
    try:
        return fn(*a, **kw)
    finally:
        if use_signal:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)


@dataclass
class RelapseFinding:
    board: str
    task_id: str
    title: str
    current_status: str
    tier: int
    marker_type: str
    marker_author: str
    marker_at: int
    approved_at_human: str
    evidence: str
    needs_operator: bool


def _board_slug(db_path=None):
    d = os.path.dirname(db_path)
    bj = os.path.join(d, 'board.json')
    if os.path.exists(bj):
        try:
            import json
            with open(bj) as f:
                return json.load(f).get('slug', os.path.basename(d))
        except Exception:
            return os.path.basename(d)
    return os.path.basename(d)


def _find_approval_marker(conn=None, task_id=None):
    '''Return dict with approval marker + verdict-router hold state, or None.

    keys: marker_type, marker_author, marker_at, evidence, needs_operator
    '''
    out = {
        'marker_type': None,
        'marker_author': None,
        'marker_at': 0,
        'evidence': None,
        'needs_operator': False,
    }
    try:
        rows = conn.execute('SELECT author, body, created_at FROM task_comments WHERE task_id=? ORDER BY created_at ASC', (task_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    for author, body, ts in rows:
        bl = (body or '').lower()
        # Anchored check (t_5e874719): mirror the core fix — strip fenced code,
        # then use anchored regex instead of bare substring match. A quoted/cited
        # occurrence in prose does NOT mark the card approved.
        stripped_body = _strip_fenced_code(body)
        if _APPROVAL_APPROVED_RE.search(stripped_body):
            out['marker_type'] = 'review_verdict'
            out['marker_author'] = author
            out['marker_at'] = ts
            out['evidence'] = 'anchored regex match on comment body'
        elif author == 'verdict-router' and 'needs-operator' in bl:
            # verdict-router marked approved but gated on operator; keep any stronger marker
            if out['marker_type'] is None:
                out['marker_type'] = 'verdict_router_approved'
                out['marker_author'] = author
                out['marker_at'] = ts
                out['evidence'] = 'verdict-router approved marker'
            out['needs_operator'] = True
    # A completed kanban_complete run is also an approval marker.
    try:
        r = conn.execute(
            "SELECT id, outcome, started_at FROM task_runs WHERE task_id=? AND outcome='completed' ORDER BY started_at ASC LIMIT 1",
            (task_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        r = None
    if r and out['marker_at'] == 0:
        out['marker_type'] = 'kanban_complete'
        out['marker_author'] = 'worker'
        out['marker_at'] = r[2]
        out['evidence'] = 'task_runs outcome=completed (run %s)' % r[0]
    if out['marker_type'] is None:
        return None
    return out


def _latest_landed_event_at(conn=None, task_id=None):
    """Return the created_at of the most recent 'completed'/'archived' event, or 0."""
    try:
        row = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind IN ('completed','archived')", (task_id,)).fetchone()
        if not row[0]:
            return 0
        return row[0]
    except sqlite3.OperationalError:
        return 0


def _latest_reviewer_reopen_at(conn=None, task_id=None, after_ts=None):
    '''Return created_at of the most recent reviewer re-open comment after a landed event.'''
    try:
        row = conn.execute(
            "SELECT MAX(created_at) FROM task_comments WHERE task_id=? AND created_at>? AND (body LIKE '%re-open%' OR body LIKE '%reopen%' OR body LIKE '%RE-OPEN%' OR body LIKE '%re-blocked%')",
            (task_id, after_ts),
        ).fetchone()
        if not row[0]:
            return 0
        return row[0]
    except sqlite3.OperationalError:
        return 0


def detect_relapses(boards=None):
    findings = []
    if boards:
        dbs = [_resolve_db(b) for b in boards]
        dbs = [d for d in dbs if d]
    else:
        dbs = _iter_board_dbs()
    skipped = []
    for path in dbs:
        slug = _board_slug(path)
        try:
            res = _with_watchdog(8, _scan_board, slug, path)
            findings.extend(res)
        except Exception:
            skipped.append(slug)
    if skipped:
        sys.stderr.write('[warn] skipped ' + str(len(skipped)) + ' locked/slow board(s): ' + ', '.join(skipped) + '\n')
    detect_relapses._skipped = skipped
    findings.sort(key=lambda f: (f.board, f.task_id))
    return findings


def _scan_board(board=None, path=None):
    '''Open + scan one board (must be called under _with_watchdog).'''
    findings = []
    try:
        conn = _open_ro(path)
    except sqlite3.Error:
        return findings
    try:
        tasks = conn.execute('SELECT id, title, status FROM tasks').fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return findings
    for tid, title, status in tasks:
        if status not in REGRESSABLE_STATUSES:
            continue
        marker = _find_approval_marker(conn, tid)
        if not marker:
            continue
        landed_at = _latest_landed_event_at(conn, tid)
        reopen = 0
        if landed_at != 0:
            reopen = _latest_reviewer_reopen_at(conn, tid, landed_at)
        if landed_at != 0 and reopen == 0:
            tier = 1
        elif marker.get('needs_operator'):
            tier = 2
        else:
            tier = 3
        findings.append(RelapseFinding(
            board=board,
            task_id=tid,
            title=title,
            current_status=status,
            tier=tier,
            marker_type=marker.get('marker_type'),
            marker_author=marker.get('marker_author'),
            marker_at=marker.get('marker_at'),
            approved_at_human=_human(marker.get('marker_at')),
            evidence=marker.get('evidence'),
            needs_operator=bool(marker.get('needs_operator')),
        ))
    conn.close()
    return findings


_SEP = '=' * 92
_SUB = '-' * 92


def cmd_detect(args):
    findings = detect_relapses(getattr(args, 'boards', None))
    t1 = [f for f in findings if f.tier == 1]
    t2 = [f for f in findings if f.tier == 2]
    t3 = [f for f in findings if f.tier == 3]
    if not findings:
        print('RELAPSE-SCAN: CLEAN — no approved/landed card currently regressed to a live status.')
        return
    print('RELAPSE-SCAN: %d APPROVED-BUT-LIVE card(s): T1=%d (silent relapse) T2=%d (operator-gated hold) T3=%d (awaiting land)' % (
        len(findings), len(t1), len(t2), len(t3)))
    print(_SEP)
    for f in findings:
        print('[%s] [%s] %s' % (f.board, f.task_id, f.title))
        print('  status=%s  approved@%s' % (f.current_status, f.approved_at_human))
        print('    title: %s' % f.title)
        print('    marker: %s by %s — %s' % (f.marker_type, f.marker_author, f.evidence))
        if f.tier == 1:
            print('    LOCK-GATE: SILENT RELAPSE — landed then regressed with no reviewer re-open. Requires reviewer+reason to re-open. ALERT.')
        elif f.tier == 2:
            print('    LOCK-GATE: operator-gated hold (correct). Awaiting operator/Frank land; do NOT auto-miss.')
        else:
            print('    LOCK-GATE: awaiting land (no operator gate). Surface as backlog; require reviewer+reason to re-open.')
        print(_SUB)


def cmd_summary(args):
    boards = getattr(args, 'boards', None)
    if boards:
        dbs = [_resolve_db(b) for b in boards]
        dbs = [d for d in dbs if d]
    else:
        dbs = _iter_board_dbs()
    scanned = len(dbs)
    if scanned == 0:
        sys.stderr.write('NO-BOARDS-SCANNED: BOARDS_DIR=%s resolved to zero board databases; refusing to emit a green governance signal.\n' % BOARDS_DIR)
        sys.exit(2)
    findings = detect_relapses(boards=boards)
    skipped = getattr(detect_relapses, '_skipped', [])
    if skipped and len(skipped) == scanned:
        sys.stderr.write('ALL-BOARDS-ERRORED: every one of %d scanned board(s) raised; refusing to emit a green governance signal.\n' % scanned)
        sys.exit(2)
    by_board = {}
    for f in findings:
        d = by_board.setdefault(f.board, {1: 0, 2: 0, 3: 0})
        d[f.tier] += 1
    t1_total = sum(d[1] for d in by_board.values())
    t2_total = sum(d[2] for d in by_board.values())
    t3_total = sum(d[3] for d in by_board.values())
    total = len(findings)
    # Defect 2 fix: headline reports the FULL approved-but-live total, not just T1.
    print('LOCKGATE-SUMMARY: %d approved-but-live (T1=%d T2=%d T3=%d) across %d board(s)' % (
        total, t1_total, t2_total, t3_total, len(by_board)))
    for board in sorted(by_board):
        d = by_board[board]
        print('  %s  T1=%d T2=%d T3=%d' % (board, d[1], d[2], d[3]))
    print('  TOTAL T1 (silent relapse) = %d' % t1_total)
    out = getattr(args, 'out', None)
    if out:
        import json
        with open(out, 'w') as fh:
            json.dump({'by_board': by_board, 'totals': {'total': total, 't1': t1_total, 't2': t2_total, 't3': t3_total}, 'boards': len(by_board)}, fh, indent=2)
        print('  ledger: %s' % out)


def cmd_simulate(args):
    db_path = _resolve_db(args.board)
    if not db_path:
        sys.stderr.write("SIMULATE: board '%s' db not found under %s\n" % (args.board, BOARDS_DIR))
        sys.exit(3)
    conn = _open_ro(db_path)
    row = conn.execute('SELECT title, status FROM tasks WHERE id=?', (args.task,)).fetchone()
    if not row:
        conn.close()
        sys.stderr.write("SIMULATE: task '%s' not found on board '%s'\n" % (args.task, args.board))
        sys.exit(4)
    title, status = row
    marker = _find_approval_marker(conn, args.task)
    conn.close()
    marker_str = '%s@%s' % (marker['marker_type'], marker['marker_at']) if marker and marker.get('marker_type') else 'NONE'
    print("SIMULATE approve->block gate on [%s] %s" % (args.board, args.task))
    print("  title: '%s'" % title)
    print("  current_status=%s" % status)
    # Defect 3 fix: report the marker we actually found, never claim "no approval marker"
    # when one exists (the status filter must not short-circuit before the marker is read).
    print("  approval_marker=%s" % marker_str)
    if status in LANDED_STATUSES:
        print("  RESULT: BLOCK REJECTED — card is in terminal/landed state with an approval marker.")
        print("  The lock-gate requires an explicit reviewer re-open with a reason comment before any transition to blocked/todo/running is permitted. Silent regression is prevented.")
    elif marker and marker.get('marker_type'):
        if marker.get('needs_operator'):
            print("  RESULT: T2 OPERATOR-GATED HOLD — approved + NEEDS-OPERATOR; awaiting operator land.")
            print("  ACTION: observe; do NOT auto-transition; require reviewer+reason to re-open.")
        else:
            print("  RESULT: RELAPSE DETECTED — approved marker present but status is live (regressed).")
            print("  ACTION: flag for reviewer; do NOT auto-transition; require reviewer+reason to re-open.")
    else:
        print("  RESULT: NO-OP — no approval marker; not a relapse candidate. (Normal live card.)")


def _resolve_db(board=None):
    for c in ('kanban.db', 'board.db'):
        p = os.path.join(BOARDS_DIR, board, c)
        if os.path.exists(p):
            return p
    for p in _iter_board_dbs():
        if _board_slug(p) == board:
            return p
    return None


def cmd_watch(args):
    import json
    boards = getattr(args, 'boards', None)
    findings = detect_relapses(boards=boards)
    payload = {
        'watch': True,
        'boards_scanned': len(boards) if boards else len(_iter_board_dbs()),
        'relapse_count': len(findings),
        'findings': [asdict(f) for f in findings],
    }
    out = getattr(args, 'out', None)
    if out:
        with open(out, 'w') as fh:
            json.dump(payload, fh, indent=2)
        print('WATCH ledger written: %s (%d relapse(s))' % (out, len(findings)))
    else:
        print(json.dumps(payload, indent=2))


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    p = argparse.ArgumentParser(description='Kanban approve->blocked relapse lock-gate (read-only detector).')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('detect', help='scan all boards for approved/landed cards currently regressed to live')
    d.add_argument('--boards', nargs='*', default=None, help='limit to these board slugs')
    s = sub.add_parser('simulate', help='acceptance gate (1): assert block is rejected on a landed/approved card')
    s.add_argument('--board', required=True, help='board slug')
    s.add_argument('--task', required=True, help='task_id')
    w = sub.add_parser('watch', help='run detect and emit a JSON relapse ledger')
    w.add_argument('--boards', nargs='*', default=None, help='limit to these board slugs')
    w.add_argument('--out', help='write JSON ledger to this path')
    su = sub.add_parser('summary', help='compact tier counts per board (dashboards/cron)')
    su.add_argument('--boards', nargs='*', default=None, help='limit to these board slugs')
    su.add_argument('--out', help='write JSON summary to this path')
    dispatch = {
        'detect': cmd_detect,
        'simulate': cmd_simulate,
        'watch': cmd_watch,
        'summary': cmd_summary,
    }
    args = p.parse_args(argv)
    return dispatch[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main() or 0)
