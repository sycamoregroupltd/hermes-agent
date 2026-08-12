#!/usr/bin/env python3
"""Wake scheduled kanban cards when their machine-readable resume gate is met.

No-agent cron script for the Jarvis profile. It scans every board under the
canonical kanban boards root and unblocks cards parked in ``status=scheduled``
when either:
- a latest schedule/comment/run marker says ``resume-at: <ISO8601>`` and that
  timestamp is in the past, or
- a latest marker says ``resume-on: parent-done`` and every linked parent task is
  done/archived.

Cards with no parseable wake condition are commented once with a PM-triage
marker so the hourly PM board triage loop can decide the right gate without the
scanner guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_BOARDS_ROOT = Path("/home/frank/.hermes/kanban/boards")
AUTHOR = "scheduled-wake-scanner"
TRIAGE_MARKER = "SCHEDULED-WAKE-TRIAGE"
UNBLOCK_MARKER = "SCHEDULED-WAKE-UNBLOCK"
COMMENT_SCAN_LIMIT = 12  # retained for compatibility; no longer caps condition lookup
SQLITE_BUSY_TIMEOUT_MS = 5000
DONE_STATUSES = {"done", "archived"}

_RESUME_AT_RE = re.compile(r"\bresume-at\s*:\s*([^\s`'\"<>]+)", re.IGNORECASE)
_RESUME_ON_PARENT_RE = re.compile(r"\bresume-on\s*:\s*parent-done\b", re.IGNORECASE)

# Cheap SQL prefilter mirroring the two regexes above. Both regexes require the
# literal hyphenated tokens `resume-at` / `resume-on`, so the LIKE is an exact
# superset: it only narrows the rows the regexes then adjudicate, and can never
# widen what counts as a condition. SQLite LIKE is ASCII case-insensitive, which
# matches the regexes' re.IGNORECASE.
_MARKER_SQL_PREDICATE = "{col} LIKE '%resume-at%' OR {col} LIKE '%resume-on%'"


class ScannerError(RuntimeError):
    """Raised for deterministic scanner failures."""


@dataclass(frozen=True)
class WakeCondition:
    kind: str
    source: str
    due_at: float | None = None
    due_text: str | None = None


@dataclass(frozen=True)
class ScheduledTask:
    board: str
    id: str
    title: str
    assignee: str | None
    priority: int


def connect(db_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        )
    else:
        conn = sqlite3.connect(str(db_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    required = {
        "tasks": {"id", "title", "assignee", "status", "priority", "current_run_id", "consecutive_failures", "last_failure_error"},
        "task_comments": {"id", "task_id", "author", "body", "created_at"},
        "task_events": {"task_id", "run_id", "kind", "payload", "created_at"},
        "task_links": {"parent_id", "child_id"},
    }
    for table, cols in required.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        have = {row[1] for row in rows}
        missing = cols - have
        if missing:
            raise ScannerError(f"{table} missing required columns: {sorted(missing)}")


def parse_iso8601(text: str) -> float | None:
    raw = text.strip().rstrip(".,;)]")
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def extract_condition_from_text(text: str, source: str) -> WakeCondition | None:
    if not text:
        return None
    # Prefer explicit resume-at when both appear in a noisy comment.
    match = _RESUME_AT_RE.search(text)
    if match:
        token = match.group(1)
        due = parse_iso8601(token)
        if due is not None:
            return WakeCondition(kind="resume-at", source=source, due_at=due, due_text=token)
    if _RESUME_ON_PARENT_RE.search(text):
        return WakeCondition(kind="parent-done", source=source)
    return None


def latest_condition(conn: sqlite3.Connection, task_id: str) -> WakeCondition | None:
    """Return the newest parseable wake condition on a card.

    Blind-spot fix (2026-08-02, kanban t_fae47050): this used to read only the
    ``COMMENT_SCAN_LIMIT`` most-recent comments. On a long-lived card the
    original hold comment scrolls out of that window, so a REAL future
    ``resume-at`` becomes invisible and the card is mis-flagged
    ``SCHEDULED-WAKE-TRIAGE`` as if it were un-gated — and, worse, an un-gated
    promotion path can then walk it out of ``scheduled``. Observed on
    sycode-trading/t_6d00d9ae, whose 2026-07-11 ``resume-at 2026-08-11`` sat
    ~16 comments back.

    We now let SQL do the filtering: only rows whose body actually contains a
    resume marker are considered, newest-first, with no window cap. The regex
    still has the final say, so the LIKE is a cheap prefilter and never widens
    what counts as a condition.
    """
    comments = conn.execute(
        f"""
        SELECT id, author, body
        FROM task_comments
        WHERE task_id = ?
          AND author IS NOT ?
          AND ({_MARKER_SQL_PREDICATE.format(col='body')})
        ORDER BY created_at DESC, id DESC
        """,
        (task_id, AUTHOR),
    ).fetchall()
    for row in comments:
        # Ignore this scanner's own comments. Its triage/wake explanations name
        # the supported syntax and must not become the next tick's wake gate.
        if (row["author"] or "") == AUTHOR:
            continue
        condition = extract_condition_from_text(row["body"] or "", f"comment#{row['id']}")
        if condition:
            return condition

    events = conn.execute(
        f"""
        SELECT id, kind, payload
        FROM task_events
        WHERE task_id = ? AND kind IN ('scheduled', 'commented')
          AND ({_MARKER_SQL_PREDICATE.format(col='payload')})
        ORDER BY created_at DESC, id DESC
        """,
        (task_id,),
    ).fetchall()
    for row in events:
        payload = row["payload"] or ""
        texts = [payload]
        try:
            decoded = json.loads(payload)
        except Exception:
            decoded = None
        if isinstance(decoded, dict):
            texts.extend(str(decoded.get(k) or "") for k in ("reason", "body", "summary"))
        for text in texts:
            condition = extract_condition_from_text(text, f"event#{row['id']}:{row['kind']}")
            if condition:
                return condition
    return None


def parent_statuses(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    return [
        (str(row["id"]), str(row["status"]))
        for row in conn.execute(
            """
            SELECT p.id, p.status
            FROM task_links AS l
            JOIN tasks AS p ON p.id = l.parent_id
            WHERE l.child_id = ?
            ORDER BY p.created_at ASC, p.id ASC
            """,
            (task_id,),
        ).fetchall()
    ]


def condition_is_met(conn: sqlite3.Connection, task_id: str, condition: WakeCondition, now_ts: float) -> tuple[bool, str]:
    if condition.kind == "resume-at":
        assert condition.due_at is not None
        return condition.due_at <= now_ts, f"resume-at {condition.due_text} from {condition.source}"
    if condition.kind == "parent-done":
        parents = parent_statuses(conn, task_id)
        if not parents:
            return False, f"resume-on parent-done from {condition.source}; no linked parents"
        undone = [f"{pid}:{status}" for pid, status in parents if status not in DONE_STATUSES]
        if undone:
            return False, f"resume-on parent-done from {condition.source}; waiting on {', '.join(undone)}"
        return True, f"resume-on parent-done from {condition.source}; all parents done/archived"
    return False, f"unknown condition {condition.kind}"


def comment_exists(conn: sqlite3.Connection, task_id: str, marker: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM task_comments
        WHERE task_id = ? AND body LIKE ?
        LIMIT 1
        """,
        (task_id, f"%{marker}%"),
    ).fetchone()
    return row is not None


def append_comment(conn: sqlite3.Connection, task_id: str, body: str, now: int) -> None:
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, AUTHOR, body, now),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'commented', ?, ?)",
        (task_id, json.dumps({"author": AUTHOR, "body_len": len(body)}, sort_keys=True), now),
    )


def unblock_task(conn: sqlite3.Connection, task_id: str, reason: str, now: int) -> str | None:
    stale = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'scheduled'",
        (task_id,),
    ).fetchone()
    if stale is None:
        return None
    if stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, 'scheduled wake scanner recovery'),
                   ended_at = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (now, int(stale["current_run_id"])),
        )
    undone = conn.execute(
        """
        SELECT 1
        FROM task_links AS l
        JOIN tasks AS p ON p.id = l.parent_id
        WHERE l.child_id = ? AND p.status != 'done'
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    new_status = "todo" if undone else "ready"
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = ?, current_run_id = NULL,
               consecutive_failures = 0, last_failure_error = NULL
         WHERE id = ? AND status = 'scheduled'
        """,
        (new_status, task_id),
    )
    if cur.rowcount != 1:
        return None
    append_comment(conn, task_id, f"{UNBLOCK_MARKER}: {reason}; moved to {new_status}.", now)
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'unblocked', ?, ?)",
        (task_id, json.dumps({"status": new_status, "reason": reason, "actor": AUTHOR}, sort_keys=True), now),
    )
    return new_status


def scheduled_tasks(conn: sqlite3.Connection, board: str) -> list[ScheduledTask]:
    rows = conn.execute(
        """
        SELECT id, title, assignee, priority
        FROM tasks
        WHERE status = 'scheduled'
        ORDER BY priority DESC, created_at ASC, id ASC
        """
    ).fetchall()
    return [
        ScheduledTask(
            board=board,
            id=str(row["id"]),
            title=str(row["title"]),
            assignee=row["assignee"],
            priority=int(row["priority"] or 0),
        )
        for row in rows
    ]


def scan_board(db_path: Path, *, now_ts: float, dry_run: bool = False, flag_missing: bool = True) -> list[str]:
    board = db_path.parent.name
    now = int(now_ts)
    actions: list[str] = []
    with connect(db_path) as conn:
        ensure_schema(conn)
        for task in scheduled_tasks(conn, board):
            condition = latest_condition(conn, task.id)
            if condition is None:
                if flag_missing and not comment_exists(conn, task.id, TRIAGE_MARKER):
                    msg = (
                        f"{TRIAGE_MARKER}: scheduled card has no parseable `resume-at: <ISO8601>` "
                        "or `resume-on: parent-done` condition in latest schedule/comment markers. "
                        "PM triage should add the wake condition or reclassify the gate."
                    )
                    if dry_run:
                        actions.append(f"DRY-RUN flag-missing {board}/{task.id}")
                    else:
                        with conn:
                            append_comment(conn, task.id, msg, now)
                        actions.append(f"FLAGGED {board}/{task.id} no-parseable-condition")
                continue
            met, reason = condition_is_met(conn, task.id, condition, now_ts)
            if not met:
                continue
            if dry_run:
                actions.append(f"DRY-RUN wake {board}/{task.id}: {reason}")
                continue
            with conn:
                new_status = unblock_task(conn, task.id, reason, now)
            if new_status:
                actions.append(f"WOKE {board}/{task.id} -> {new_status}: {reason}")
    return actions


def scan_all(boards_root: Path, *, dry_run: bool = False, flag_missing: bool = True) -> list[str]:
    now_ts = time.time()
    actions: list[str] = []
    for db_path in sorted(boards_root.glob("*/kanban.db")):
        try:
            actions.extend(scan_board(db_path, now_ts=now_ts, dry_run=dry_run, flag_missing=flag_missing))
        except sqlite3.OperationalError as exc:
            actions.append(f"ERROR {db_path.parent.name}: sqlite operational error: {exc}")
        except ScannerError as exc:
            actions.append(f"ERROR {db_path.parent.name}: {exc}")
    return actions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--boards-root", type=Path, default=DEFAULT_BOARDS_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-flag-missing", action="store_true")
    args = parser.parse_args(argv)

    actions = scan_all(
        args.boards_root,
        dry_run=args.dry_run,
        flag_missing=not args.no_flag_missing,
    )
    errors = [line for line in actions if line.startswith("ERROR ")]
    if actions:
        print("\n".join(actions))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
