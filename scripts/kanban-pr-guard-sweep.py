#!/usr/bin/env python3
"""
kanban-pr-guard-sweep.py — Transition active_pr-guarded ready tasks to review status.

Mechanism: board-mechanism (no_agent cron script)
Producer: This script, when it transitions tasks
Consumer: PM triage sweep (Gap 5), dispatcher (freed ready queue)
Where-installed: /home/frank/.hermes/scripts/kanban-pr-guard-sweep.py
Schedule: every 30 minutes (cron row on a LIVE gateway profile — jarvis-voice)
A-Tier: A1 (reversible board mutation, evidence via task_events)

Logic:
- Find ready tasks with claim_lock IS NULL
- Check if they have a GitHub PR URL in recent comments (active_pr guard)
- Resolve the ACTUAL GitHub PR state (read-only `gh pr view`): only a still-OPEN
  PR keeps a card stuck in the ready queue. A MERGED/CLOSED PR must NOT be
  moved — the dispatcher's PR-state-aware respawn guard (t_9799c507) lets those
  cards spawn normally. Unknown state (gh failure) is skipped (fail closed: do
  not churn cards on uncertain data).
- If guarded by an OPEN PR for >24 hours, transition status from 'ready' to
  'review' — removes the card from the dispatcher's ready queue so PM triage
  (Gap 5) can pick it up.

Run modes:
  kanban-pr-guard-sweep.py            mutate (scheduled)
  kanban-pr-guard-sweep.py --dry-run  report what WOULD transition; no writes
  kanban-pr-guard-sweep.py --board sycode-trading   single board (selftest)
"""

import argparse
import os
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

# Boards root. Override via KANBAN_PR_GUARD_BOARDS_ROOT for selftest/dry-run
# against a scratch fixture (never change in the scheduled cron environment).
BOARDS_ROOT = Path(
    os.environ.get("KANBAN_PR_GUARD_BOARDS_ROOT", "/home/frank/.hermes/kanban/boards")
)

# How long a task can sit in ready with an OPEN-PR guard before we move it to review
PR_GUARD_THRESHOLD_SECONDS = 86400  # 24 hours

# How far back to look for PR URLs in comments (>= the dispatcher's guard window)
PR_LOOKBACK_SECONDS = 86400 * 7  # 7 days

# GitHub PR URL pattern with capture groups (owner, repo, number)
PR_URL_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)", re.I)


def pr_refs_in_comments(conn, task_id: str, now: int) -> "list[tuple[str, str, str]]":
    """Return (owner, repo, number) tuples for PR URLs in recent comments."""
    cutoff = now - PR_LOOKBACK_SECONDS
    refs: "list[tuple[str, str, str]]" = []
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, cutoff),
    ).fetchall()
    for row in rows:
        body = row[0] if not hasattr(row, "keys") else row["body"]
        if body:
            for m in PR_URL_RE.finditer(body):
                refs.append((m.group(1), m.group(2), m.group(3)))
    return refs


def any_pr_still_open(refs: "list[tuple[str, str, str]]") -> bool:
    """True if at least one referenced PR resolves to a still-OPEN state.

    Fail closed: an unresolved state (gh missing / network / unknown) counts as
    open so the sweep does NOT move a card whose PR may genuinely be blocking.
    MERGED/CLOSED PRs never count — those cards belong in the ready queue now
    that the dispatcher guard is PR-state-aware (t_9799c507).
    """
    for owner, repo, number in refs:
        state = kb._github_pr_state(f"{owner}/{repo}", number)
        if state in ("MERGED", "CLOSED"):
            continue
        return True  # OPEN or unknown -> keep the rescue path interested
    return False


def last_pr_comment_time(conn, task_id: str) -> int:
    """Timestamp of the most recent comment mentioning a GitHub PR URL."""
    row = conn.execute(
        "SELECT MAX(created_at) FROM task_comments WHERE task_id = ? AND body LIKE '%github.com%pull%'",
        (task_id,),
    ).fetchone()
    val = row[0] if row else None
    if isinstance(val, dict) or hasattr(val, "keys"):
        val = row["MAX(created_at)"] if row else None
    return int(val) if val else 0


def sweep_board(board: str, dry_run: bool) -> "list[dict]":
    """Sweep a board for OPEN-PR-guarded ready tasks. Returns list of transitioned tasks."""
    db_path = BOARDS_ROOT / board / "kanban.db"
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
            task_id = row["id"]
            refs = pr_refs_in_comments(conn, task_id, now)
            if not refs:
                continue

            # Age gate FIRST (cheap SQL) — only cards PR-guarded >24h are rescue
            # candidates. We resolve GitHub PR state (subprocess, slow) only for
            # these, never for every ready card with a PR mention.
            last_pr_ts = last_pr_comment_time(conn, task_id)
            if last_pr_ts == 0:
                continue
            guard_duration = now - last_pr_ts
            if guard_duration < PR_GUARD_THRESHOLD_SECONDS:
                continue

            # Only cards whose PR is still OPEN (or unverifiable) are stuck here.
            if not any_pr_still_open(refs):
                continue

            entry = {
                "task_id": task_id,
                "assignee": row["assignee"],
                "title": (row["title"] or "")[:60],
                "pr_age_hours": guard_duration // 3600,
            }

            if dry_run:
                transitioned.append(entry)
                continue

            # Transition to review (A1 reversible board mutation; rollback = set status back)
            conn.execute(
                "UPDATE tasks SET status = 'review', block_kind = 'dependency' WHERE id = ?",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, created_at, payload) "
                "VALUES (?, 'status', ?, ?)",
                (
                    task_id,
                    now,
                    '{"from":"ready","to":"review","reason":"active_pr_guard_sweep","pr_age_hours":%d}'
                    % (guard_duration // 3600),
                ),
            )
            transitioned.append(entry)

        if not dry_run:
            conn.commit()

    finally:
        conn.close()

    return transitioned


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transition active_pr-guarded ready tasks to review status"
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; make no board changes")
    parser.add_argument("--board", help="Sweep only this board slug (default: all configured boards)")
    args = parser.parse_args()

    boards = [args.board] if args.board else BOARDS
    all_transitioned = []

    for board in boards:
        if board not in BOARDS:
            print(f"PR GUARD SWEEP: unknown board {board!r}; allowed={BOARDS}")
            return 2
        transitioned = sweep_board(board, dry_run=args.dry_run)
        for t in transitioned:
            t["board"] = board
            all_transitioned.append(t)

    mode = "DRY-RUN" if args.dry_run else "SWEEP"
    if all_transitioned:
        print(f"PR GUARD {mode}: {len(all_transitioned)} ready+OPEN-PR task(s) -> review")
        for t in all_transitioned:
            print(f"  {t['board']}/{t['task_id']} [{t['assignee']}] {t['title']} (PR open {t['pr_age_hours']}h)")
        # Dry-run reports are informational (exit 0). Real transitions alert (exit 1).
        return 0 if args.dry_run else 1
    return 0  # Silent no-op


if __name__ == "__main__":
    raise SystemExit(main())
