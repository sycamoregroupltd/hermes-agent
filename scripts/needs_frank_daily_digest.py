#!/usr/bin/env python3
"""Daily NEEDS-FRANK digest from fleet status and approvals registry.

Dual-delivery: stdout (picked up by Hermes cron -> Discord) + direct WhatsApp send to Frank's phone."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

FLEET_STATUS = Path('/home/frank/uaa-rules/FLEET-STATUS.md')
APPROVALS = Path('/home/frank/uaa-rules/approvals-registry.md')
BOARDS = Path('/home/frank/.hermes/kanban/boards')
WA_TARGET = os.environ.get('DIGEST_WA_TARGET', 'whatsapp:Frank')
HERMES = os.environ.get('DIGEST_HERMES_BIN', '/home/frank/.local/bin/hermes')

PENDING_RE = re.compile(r'^-\s+([a-z0-9_-]+)\s+\|\s+(t_[a-f0-9]+)\s+\|\s+(.+?)\s*$')
DATE_RE = re.compile(r'^(?:##\s+)?(\d{4}-\d{2}-\d{2})\s+[—-]\s+(.+?)\s+[—-]\s+AWAITING\b', re.I)

def read(path: Path) -> str:
    return path.read_text(errors='ignore') if path.exists() else ''

def age_days_from_unix(ts):
    if not ts:
        return None
    return int((datetime.now(timezone.utc).timestamp() - float(ts)) // 86400)

def task_info(board: str, task_id: str):
    db = BOARDS / board / 'kanban.db'
    if not db.exists():
        return {}
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        row = conn.execute('select status, assignee, created_at, block_kind from tasks where id=?', (task_id,)).fetchone()
        if not row:
            return {}
        return {'status': row[0], 'assignee': row[1], 'created_at': row[2], 'block_kind': row[3], 'age_days': age_days_from_unix(row[2])}
    finally:
        conn.close()

def pending_frank_items():
    text = read(FLEET_STATUS)
    items = []
    in_section = False
    for line in text.splitlines():
        if line.startswith('## Pending Frank'):
            in_section = True
            continue
        if in_section and line.startswith('## '):
            break
        if not in_section:
            continue
        m = PENDING_RE.match(line)
        if m:
            board, tid, title = m.groups()
            info = task_info(board, tid)
            items.append({'board': board, 'id': tid, 'title': title.strip(), **info})
    return items

def awaiting_approvals():
    items = []
    for line in read(APPROVALS).splitlines():
        m = DATE_RE.match(line.strip())
        if not m:
            continue
        date_s, title = m.groups()
        try:
            dt = datetime.fromisoformat(date_s).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).days
        except Exception:
            age = None
        items.append({'date': date_s, 'title': title.strip(), 'age_days': age})
    return items

def main():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    pending = pending_frank_items()
    awaiting = awaiting_approvals()
    lines = [f'NEEDS-FRANK digest {now}', f'Pending-Frank tasks: {len(pending)}', f'Approvals-registry AWAITING entries: {len(awaiting)}']
    lines.append('')
    lines.append('Pending-Frank oldest first:')
    if pending:
        for item in sorted(pending, key=lambda x: (x.get('age_days') is None, x.get('age_days') or 0), reverse=True)[:25]:
            age = '?' if item.get('age_days') is None else f"{item['age_days']}d"
            lines.append(f"- {item['board']} {item['id']} age={age} status={item.get('status','?')} assignee={item.get('assignee','?')} block={item.get('block_kind','?')} — {item['title']}")
    else:
        lines.append('- none')
    lines.append('')
    lines.append('Approvals awaiting:')
    if awaiting:
        for item in sorted(awaiting, key=lambda x: x.get('age_days') or -1, reverse=True):
            age = '?' if item.get('age_days') is None else f"{item['age_days']}d"
            lines.append(f"- {item['date']} age={age} — {item['title']}")
    else:
        lines.append('- none')
    body = '\n'.join(lines)
    # Discord delivery via stdout (picked up by Hermes cron deliver target)
    print(body)
    # WhatsApp dual-delivery to Frank's phone
    env = os.environ.copy()
    env['HERMES_HOME'] = '/home/frank/.hermes'
    try:
        result = subprocess.run(
            [HERMES, 'send', '-q', '-t', WA_TARGET, '-s', '📋 NEEDS-FRANK digest', body],
            capture_output=True, text=True, timeout=120, env=env,
        )
        if result.returncode != 0:
            print(f'[DIGEST-WA-FAILED] rc={result.returncode} {result.stderr.strip()[:200]}', file=__import__('sys').stderr)
    except Exception as exc:
        print(f'[DIGEST-WA-ERROR] {type(exc).__name__}: {exc}', file=__import__('sys').stderr)

if __name__ == '__main__':
    main()
