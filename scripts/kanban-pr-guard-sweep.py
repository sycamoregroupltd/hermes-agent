#!/usr/bin/env python3
"""
kanban-pr-guard-sweep.py — Transition active_pr-guarded ready tasks to review status.

Mechanism: board-mechanism (no_agent cron script)
Producer: This script, when it transitions tasks
Consumer: PM triage sweep (Gap 5), dispatcher (freed ready queue)
Where-installed: /home/frank/.hermes/scripts/kanban-pr-guard-sweep.py
Schedule: every 30 minutes
A-Tier: A1 (reversible board mutation, evidence via task_events)

Logic:
- Find ready tasks with claim_lock IS NULL
- Check if they have a GitHub PR URL in recent comments (active_pr guard)
- If guarded for >24 hours, transition status from 'ready' to 'review'
- This removes them from the dispatcher's ready queue
- PM sweep (Gap 5) will pick them up from review status
"""

import re
import sqlite3
import sys
import time
from pathlib import Path

BOARDS = [
    "sycode-trading",
    "jarvis-os",
    "upero",
    "yorkstone-supplies",
    "ai-restaurant",
]

# How long a task can sit in ready with active_pr before we move it to review
PR_GUARD_THRESHOLD_SECONDS = 86400  # 24 hours

# GitHub PR URL pattern
PR_URL_RE = re.compile(r'github\.com/[^/]+/[^/]+/pull/\d+', re.I)


def check_active_pr_guard(conn, task_id: str, now: int) -> bool:
    """Check if task has an active PR in recent comments (mirrors dispatcher guard)."""
    pr_cutoff = now - (86400 * 7)  # 7 days, same as dispatcher
    
    comments = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, pr_cutoff)
    ).fetchall()
    
    for c in comments:
        if c[0] and PR_URL_RE.search(c[0]):
            return True
    return False


def get_last_pr_comment_time(conn, task_id: str) -> int:
    """Get timestamp of most recent PR comment."""
    row = conn.execute(
        "SELECT MAX(created_at) FROM task_comments WHERE task_id = ? AND body LIKE '%github.com%pull%'",
        (task_id,)
    ).fetchone()
    return row[0] if row and row[0] else 0


def sweep_board(board: str) -> list:
    """Sweep a board for PR-guarded ready tasks. Returns list of transitioned tasks."""
    db_path = Path(f"/home/frank/.hermes/kanban/boards/{board}/kanban.db")
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    now = int(time.time())
    transitioned = []
    
    try:
        # Find ready, unclaimed tasks
        ready = conn.execute(
            "SELECT id, assignee, title FROM tasks "
            "WHERE status = 'ready' AND claim_lock IS NULL"
        ).fetchall()
        
        for row in ready:
            task_id = row['id']
            
            # Check if this task would be guarded by active_pr
            if not check_active_pr_guard(conn, task_id, now):
                continue
            
            # Check how long it's been guarded
            last_pr_time = get_last_pr_comment_time(conn, task_id)
            if last_pr_time == 0:
                continue
            
            guard_duration = now - last_pr_time
            if guard_duration < PR_GUARD_THRESHOLD_SECONDS:
                continue
            
            # Transition to review
            conn.execute(
                "UPDATE tasks SET status = 'review', block_kind = 'dependency' WHERE id = ?",
                (task_id,)
            )
            
            # Log event
            conn.execute(
                "INSERT INTO task_events (task_id, kind, created_at, payload) "
                "VALUES (?, 'status', ?, ?)",
                (task_id, now, f'{{"from":"ready","to":"review","reason":"active_pr_guard_sweep","pr_age_hours":{guard_duration//3600}}}')
            )
            
            transitioned.append({
                "task_id": task_id,
                "assignee": row['assignee'],
                "title": row['title'][:60],
                "pr_age_hours": guard_duration // 3600,
            })
        
        conn.commit()
        
    finally:
        conn.close()
    
    return transitioned


def main():
    all_transitioned = []
    
    for board in BOARDS:
        transitioned = sweep_board(board)
        for t in transitioned:
            t['board'] = board
            all_transitioned.append(t)
    
    if all_transitioned:
        print(f"PR GUARD SWEEP: transitioned {len(all_transitioned)} tasks to review")
        for t in all_transitioned:
            print(f"  {t['board']}/{t['task_id']} [{t['assignee']}] {t['title']} (PR open {t['pr_age_hours']}h)")
        sys.exit(1)  # Alert — tasks were transitioned
    else:
        sys.exit(0)  # Silent


if __name__ == "__main__":
    main()
