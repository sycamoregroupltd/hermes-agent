#!/usr/bin/env bash
set -euo pipefail
# Reports per-board momentum for Frank's morning briefing.
# advanced_24h = distinct tasks with created/claimed/review/block/complete/promote/unblock events in the last 24h.
python3 - <<'PYINNER'
import sqlite3, time
from pathlib import Path
boards = ['upero','jarvis-os','sycode-ai','sycode-trading']
root = Path('/home/frank/.hermes/kanban/boards')
cutoff = int(time.time()) - 24*3600
for board in boards:
    db = root / board / 'kanban.db'
    if not db.exists():
        print(f'{board}: missing db')
        continue
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    status_counts = {row['status']: row['n'] for row in con.execute('select status, count(*) n from tasks group by status')}
    advanced = con.execute("""
        select count(distinct task_id) n
        from task_events
        where created_at >= ?
          and kind in ('created','claimed','spawned','blocked','completed','unblocked','promoted','assigned')
    """, (cutoff,)).fetchone()['n']
    done24 = con.execute("select count(distinct task_id) n from task_events where created_at >= ? and kind='completed'", (cutoff,)).fetchone()['n']
    started24 = con.execute("select count(distinct task_id) n from task_events where created_at >= ? and kind in ('claimed','spawned')", (cutoff,)).fetchone()['n']
    created24 = con.execute("select count(distinct task_id) n from task_events where created_at >= ? and kind='created'", (cutoff,)).fetchone()['n']
    active = sum(status_counts.get(s, 0) for s in ('ready','running','review'))
    backlog = sum(status_counts.get(s, 0) for s in ('todo','ready','running','review','blocked','scheduled','triage'))
    compliance = 'OK' if (backlog == 0 or active >= 1) else 'NEEDS_NEXT_TASK'
    print(f"{board}: advanced_24h={advanced} created={created24} started={started24} completed={done24} active_ready_running_review={active} backlog={backlog} compliance={compliance} statuses={dict(sorted(status_counts.items()))}")
    con.close()
PYINNER
