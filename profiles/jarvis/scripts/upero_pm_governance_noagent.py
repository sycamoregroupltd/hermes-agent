#!/usr/bin/env python3
"""No-agent Upero PM governance watchdog.

Uses sqlite3 to read/write the upero kanban board for task lifecycle
management. Follows strict NO skill-write / NO code-edit rules.

Contract (from job definition):
  1. Read NORTH-STAR.md + PROGRESS.md before acting
  2. Scan ready/todo tasks; assign unassigned ones
  3. Comment on tasks running >2h without status update
  4. Close tasks with REVIEW_VERDICT=APPROVE
  5. Promote highest-priority todo -> ready if board has no ready tasks
  6. Do NOT touch running tasks unless silent >2h
  7. Do NOT create new tasks unless board is empty of ready/todo/running
  8. ABSOLUTE SKILL-WRITE BAN: no skill_manage, curator writes, or patch/write_file
  9. CRON PROBE SAFETY: no execute_code; terminal/hermes kanban CLI/sqlite3 only

Silent on success (no-agent: empty stdout = no delivery).
Delivers to local channel.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

NORTH_STAR = Path("/home/frank/jarvis/workspace/goals/upero/NORTH-STAR.md")
PROGRESS_MD = Path("/home/frank/jarvis/workspace/goals/upero/PROGRESS.md")

HERMES_LOCAL_BIN = Path("/home/frank/.local/bin/hermes")
KANBAN_DB = None  # resolved dynamically below


def resolve_kanban_db() -> Path | None:
    """Try to find the upero kanban db."""
    candidates = [
        Path("/home/frank/.hermes/kanban.db"),
        Path("/home/frank/.hermes/boards/upero.kanban.db"),
    ]
    for c in candidates:
        if c.exists():
            return c
    # Try via hermes CLI if available
    if HERMES_LOCAL_BIN.exists():
        try:
            cp = subprocess.run(
                [str(HERMES_LOCAL_BIN), "kanban", "board-path", "--name", "upero"],
                capture_output=True, text=True, timeout=10,
            )
            if cp.returncode == 0:
                p = cp.stdout.strip()
                if p:
                    return Path(p)
        except Exception:
            pass
    return None


def db_query(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    """Run a sqlite3 query safely."""
    cmd = ["sqlite3", "-json", str(db), sql]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if cp.returncode != 0:
        print(f"DB error: {cp.stderr.strip()}", file=sys.stderr)
        return []
    try:
        return json.loads(cp.stdout) if cp.stdout.strip() else []
    except json.JSONDecodeError:
        return []


def db_exec(db: Path, sql: str, args: tuple = ()) -> int:
    """Execute a sql statement; return rows affected."""
    cmd = ["sqlite3", str(db)]
    cp = subprocess.run(
        cmd, input=sql, capture_output=True, text=True, timeout=30
    )
    return cp.returncode


def get_now_epoch() -> int:
    return int(time.time())


def format_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def main() -> dict:
    global KANBAN_DB
    KANBAN_DB = resolve_kanban_db()
    if not KANBAN_DB:
        print("ERROR: could not locate upero kanban database", file=sys.stderr)
        return {"error": "no kanban db found"}

    timestamp = datetime.now(timezone.utc).isoformat()
    actions_taken: list[dict] = []
    now_epoch = get_now_epoch()
    TWO_HOURS = 2 * 3600

    # 1. Read goal files (informational — don't mutate)
    goals_summary = {}
    for gp, label in [(NORTH_STAR, "NORTH-STAR"), (PROGRESS_MD, "PROGRESS")]:
        try:
            goals_summary[label] = gp.read_text(errors="replace")[:200]
        except FileNotFoundError:
            goals_summary[label] = "NOT FOUND"

    # 2. Query board state
    # Get all tasks
    tasks_raw = db_query(
        KANBAN_DB,
        "SELECT id, title, assignee, status, priority, "
        "created_at, started_at, last_heartbeat_at, last_failure_error, "
        "block_kind, consecutive_failures, result "
        "FROM tasks ORDER BY priority DESC, created_at ASC"
    )

    tasks = []
    for t in tasks_raw:
        task_dict = dict(t) if isinstance(t, dict) else {}
        # If we got tuples (depends on sqlite3 version/json mode), convert
        if not task_dict and len(t) >= 12:
            cols = ["id","title","assignee","status","priority","created_at",
                    "started_at","last_heartbeat_at","last_failure_error",
                    "block_kind","consecutive_failures","result"]
            task_dict = dict(zip(cols, t))
        elif not task_dict:
            task_dict = dict(t)
        tasks.append(task_dict)

    ready_tasks = [t for t in tasks if t.get("status") == "ready"]
    todo_tasks = [t for t in tasks if t.get("status") == "todo"]
    running_tasks = [t for t in tasks if t.get("status") == "running"]

    # 3. Assign unassigned ready tasks (look for common profiles)
    known_profiles = [
        "upero-pm", "devops", "yorkstone-supplies-pm",
        "frankspencer", "reviewer", "writer"
    ]

    for task in ready_tasks:
        if not task.get("assignee"):
            # Assign to default: upero-pm
            action_sql = f'UPDATE tasks SET assignee="upero-pm" WHERE id="{task["id"]}"'
            rc = db_exec(KANBAN_DB, action_sql)
            if rc == 0:
                actions_taken.append({
                    "action": "assigned_ready_to_upero-pm",
                    "task_id": task["id"],
                    "task_title": task.get("title", ""),
                })

    # 4. Check running tasks for silence (>2h with no heartbeat)
    for task in running_tasks:
        heartbeat = task.get("last_heartbeat_at")
        started = task.get("started_at")
        should_check = False

        if heartbeat:
            try:
                hb_ts = int(heartbeat)
                if now_epoch - hb_ts > TWO_HOURS:
                    should_check = True
            except (ValueError, TypeError):
                pass
        elif started:
            try:
                start_ts = int(started)
                if now_epoch - start_ts > TWO_HOURS:
                    should_check = True
            except (ValueError, TypeError):
                pass

        if should_check:
            comment_sql = f'''INSERT INTO task_comments (task_id, body, created_at) VALUES ("{task['id']}", "Upero-pm-governance: task running >2h with no status update. Please provide progress.", "{datetime.now(timezone.utc).isoformat()}")'''
            rc = db_exec(KANBAN_DB, comment_sql)
            actions_taken.append({
                "action": "commented_on_silent_running",
                "task_id": task["id"],
                "task_title": task.get("title", ""),
            })

    # 5. Check for tasks with APPROVED verdicts — close as done
    # Search comments for REVIEW_VERDICT pattern
    approved_ids = set()
    for task in tasks:
        comments_raw = db_query(
            KANBAN_DB,
            f'SELECT body FROM task_comments WHERE task_id="{task["id"]}"'
        )
        for row in comments_raw:
            if isinstance(row, dict):
                body = row.get("body", "")
            elif isinstance(row, (list, tuple)) and len(row) >= 1:
                body = str(row[0])
            else:
                body = str(row)
            if "REVIEW_VERDICT" in body.upper() and "APPROVE" in body.upper():
                approved_ids.add(task["id"])

    for task_id in approved_ids:
        task = next((t for t in tasks if t.get("id") == task_id), None)
        if task:
            # Only close if currently 'review' or 'approved' status
            current_status = task.get("status", "")
            if current_status in ("review", "approved"):
                set_done_sql = f'UPDATE tasks SET status="done", completed_at="{datetime.now(timezone.utc).isoformat()}" WHERE id="{task_id}"'
                rc = db_exec(KANBAN_DB, set_done_sql)
                if rc == 0:
                    actions_taken.append({
                        "action": "closed_approved_task",
                        "task_id": task_id,
                        "task_title": task.get("title", ""),
                    })

    # 6. Promote todo → ready if no ready tasks exist
    if not ready_tasks and todo_tasks:
        # Sort by priority DESC then created_at ASC
        sorted_todos = sorted(todo_tasks, key=lambda t: (-t.get("priority", 0), t.get("created_at", "")))
        if sorted_todos:
            best_todo = sorted_todos[0]
            promote_sql = f'UPDATE tasks SET status="ready" WHERE id="{best_todo["id"]}"'
            rc = db_exec(KANBAN_DB, promote_sql)
            if rc == 0:
                actions_taken.append({
                    "action": "promoted_todo_to_ready",
                    "task_id": best_todo["id"],
                    "task_title": best_todo.get("title", ""),
                })

    # 7. Report
    report = {
        "timestamp": timestamp,
        "goals_read": goals_summary,
        "tasks_total": len(tasks),
        "tasks_by_status": {
            "ready": len(ready_tasks),
            "todo": len(todo_tasks),
            "running": len(running_tasks),
        },
        "actions_taken": actions_taken,
        "db_path": str(KANBAN_DB),
    }

    if actions_taken:
        print(json.dumps(report, indent=2))

    return report


if __name__ == "__main__":
    try:
        result = main()
        sys.exit(0)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
