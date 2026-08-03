#!/usr/bin/env python3
"""
kanban-review-transition.py — Transition ready tasks with review-required PR comments to review status.

Mechanism: board-mechanism (no_agent cron script)
Producer: This script, when it transitions tasks
Consumer: PM triage sweep (Gap 5), dispatcher (freed ready queue)
Where-installed: /home/frank/.hermes/scripts/kanban-review-transition.py
Schedule: every 15 minutes
A-Tier: A1 (reversible board mutation, evidence via task_events)

Logic:
- Find ready tasks with claim_lock IS NULL
- Check if they have a recent "review-required" or "REVIEW-REQUIRED" comment with a PR URL
- If yes, transition status from 'ready' to 'review'
- This removes them from the dispatcher's ready queue
- PM sweep (Gap 5) will pick them up from review status
"""

import re
import sys
import time
from pathlib import Path
from hermes_cli import kanban_db as kb

BOARDS = [
    "sycode-trading",
    "jarvis-os",
    "upero",
    "yorkstone-supplies",
    "ai-restaurant",
]

# GitHub PR URL pattern
PR_URL_RE = re.compile(r'github\.com/[^/]+/[^/]+/pull/\d+', re.I)

# Review-required markers
REVIEW_REQUIRED_RE = re.compile(r'review.?required|REVIEW.?REQUIRED|REVIEW_VERDICT|review.handoff', re.I)


def should_transition(conn, task_id: str, now: int) -> tuple:
    """Check if task should transition to review. Returns (should_transition, reason, age_hours)."""
    # Look for review-required comment with PR URL in last 48 hours
    cutoff = now - (86400 * 2)  # 48 hours
    
    comments = conn.execute(
        "SELECT body, created_at FROM task_comments WHERE task_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (task_id, cutoff)
    ).fetchall()
    
    for body, created_at in comments:
        if not body:
            continue
        # Check if it's a review-required handoff with PR
        if REVIEW_REQUIRED_RE.search(body) and PR_URL_RE.search(body):
            age_hours = (now - created_at) // 3600
            return True, "review_required_with_pr", age_hours
    
    return False, None, 0


def sweep_board(board: str) -> list:
    """Sweep a board for review-required ready tasks. Returns list of transitioned tasks."""
    db_path = Path(f"/home/frank/.hermes/kanban/boards/{board}/kanban.db")
    if not db_path.exists():
        return []

    conn = kb.connect(db_path=db_path)
    
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
            
            should, reason, age_hours = should_transition(conn, task_id, now)
            if not should:
                continue
            
            # Transition to review
            conn.execute(
                "UPDATE tasks SET status = 'review', block_kind = 'needs_input' WHERE id = ?",
                (task_id,)
            )
            
            # Log event
            conn.execute(
                "INSERT INTO task_events (task_id, kind, created_at, payload) "
                "VALUES (?, 'status', ?, ?)",
                (task_id, now, f'{{"from":"ready","to":"review","reason":"{reason}","age_hours":{age_hours}}}')
            )
            
            transitioned.append({
                "task_id": task_id,
                "assignee": row['assignee'],
                "title": row['title'][:60],
                "age_hours": age_hours,
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
        print(f"REVIEW TRANSITION: moved {len(all_transitioned)} tasks to review")
        for t in all_transitioned:
            print(f"  {t['board']}/{t['task_id']} [{t['assignee']}] {t['title']} (handoff {t['age_hours']}h ago)")
        sys.exit(1)  # Alert — tasks were transitioned
    else:
        sys.exit(0)  # Silent


if __name__ == "__main__":
    main()
