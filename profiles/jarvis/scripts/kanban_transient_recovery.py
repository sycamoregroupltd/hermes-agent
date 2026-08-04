#!/usr/bin/env python3
"""Recover transient/provider-transient kanban blockers.

No-agent Jarvis cron helper. It performs deterministic SQLite transitions only:
- provider-sweep: unblock provider-transient cards after an upstream provider
  outage clears (called by codex_exhaustion_circuit_breaker on RECOVERED).
- cooldown: unblock block_kind='transient' cards after a cooldown, with a
  bounded retry count before flipping the blocker to needs_input.

Empty stdout means no action. Non-empty stdout is intended for cron delivery.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_BOARD_DIR = Path("/home/frank/.hermes/kanban/boards")
AUTHOR = "kanban-transient-recovery"
TRANSIENT_MARKER = "TRANSIENT-RETRY-AUTO"
PROVIDER_MARKER = "PROVIDER-TRANSIENT-RECOVERY-SWEEP"


@dataclass(frozen=True)
class BlockedTask:
    board: str
    db_path: Path
    task_id: str
    title: str
    assignee: str | None
    status: str
    block_kind: str | None
    block_recurrences: int
    last_failure_error: str | None
    latest_block_at: int | None
    latest_block_reason: str


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    required = {
        "tasks": {"id", "title", "assignee", "status", "block_kind", "block_recurrences", "last_failure_error", "current_run_id"},
        "task_events": {"id", "task_id", "kind", "payload", "created_at"},
        "task_comments": {"task_id", "author", "body", "created_at"},
        "task_links": {"parent_id", "child_id"},
    }
    for table, cols in required.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing = cols - have
        if missing:
            raise RuntimeError(f"{table} missing required columns: {sorted(missing)}")


def append_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
        (task_id, kind, json.dumps(payload, sort_keys=True) if payload else None, int(time.time())),
    )


def add_comment(conn: sqlite3.Connection, task_id: str, body: str) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (task_id, AUTHOR, body.strip(), now),
    )
    append_event(conn, task_id, "commented", {"author": AUTHOR, "len": len(body)})


# Kinds that explicitly record a "this card is now blocked" signal.
EXPLICIT_BLOCK_KINDS = ("blocked", "dependency_wait", "block_loop_detected")
# Kinds that freeze a card via the dispatcher (e.g. crash-loop `gave_up`)
# WITHOUT ever emitting an explicit `blocked` event. These cards still carry
# status='blocked' + block_kind='transient' and must not be skipped by the
# cooldown path, otherwise recovery silently no-ops on them.
FREEZE_KINDS = ("gave_up", "crashed", "reclaimed", "claimed")


def _reason_from_payload(payload: str | None) -> str:
    reason = ""
    try:
        data = json.loads(payload or "{}")
        if isinstance(data, dict):
            reason = str(data.get("reason") or data.get("kind") or "")
    except Exception:
        reason = ""
    return reason


def latest_explicit_block_signal(conn: sqlite3.Connection, task_id: str) -> tuple[int | None, str]:
    """The card's most recent EXPLICIT block signal (blocked / dependency_wait /
    block_loop_detected). Returns (created_at, reason) or (None, '') when the card
    was never given an explicit blocked event."""
    row = conn.execute(
        """
        SELECT created_at, payload
        FROM task_events
        WHERE task_id = ?
          AND kind IN ('blocked', 'dependency_wait', 'block_loop_detected')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        return None, ""
    return int(row["created_at"]), _reason_from_payload(row["payload"])


def latest_block_signal(conn: sqlite3.Connection, task_id: str, last_heartbeat_at: int | None = None) -> tuple[int | None, str]:
    """Effective block-signal time for a card.

    Precedence (latest wins):
      1. explicit blocked/dependency_wait/block_loop_detected event
      2. freeze event (gave_up/crashed/reclaimed/claimed) — dispatcher-frozen
         cards carry status='blocked', block_kind='transient' but emit NO explicit
         `blocked` event, so without this fallback their age is None and the
         cooldown path silently skips them (the 2026-07-09 blind spot).
      3. tasks.last_heartbeat_at (final safety net: the last time the card was alive)

    Returns (created_at, reason). reason notes the source when a fallback was used.
    """
    at, reason = latest_explicit_block_signal(conn, task_id)
    if at is not None:
        return at, reason

    row = conn.execute(
        """
        SELECT created_at, kind, payload
        FROM task_events
        WHERE task_id = ?
          AND kind IN ('gave_up', 'crashed', 'reclaimed', 'claimed')
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if row is not None:
        at = int(row["created_at"])
        base_reason = _reason_from_payload(row["payload"]) or row["kind"]
        return at, f"freeze:{row['kind']}:{base_reason}"

    if last_heartbeat_at is not None:
        return int(last_heartbeat_at), "freeze:last_heartbeat_at"
    return None, ""


def recent_comment_text(conn: sqlite3.Connection, task_id: str, limit: int = 5) -> str:
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (task_id, limit),
    ).fetchall()
    return "\n".join(str(r["body"] or "") for r in rows)


def retry_comment_count(conn: sqlite3.Connection, task_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ? AND body LIKE ?",
        (task_id, f"%{TRANSIENT_MARKER}%"),
    ).fetchone()
    return int(row["n"] if row else 0)


def iter_boards(board_dir: Path, only: Iterable[str] | None = None) -> Iterable[tuple[str, Path]]:
    allowed = set(only or [])
    for child in sorted(board_dir.iterdir() if board_dir.exists() else []):
        if not child.is_dir():
            continue
        if allowed and child.name not in allowed:
            continue
        db_path = child / "kanban.db"
        if db_path.is_file():
            yield child.name, db_path


def discover_blocked(board_dir: Path, boards: Iterable[str] | None = None) -> list[BlockedTask]:
    tasks: list[BlockedTask] = []
    for board, db_path in iter_boards(board_dir, boards):
        with connect(db_path) as conn:
            ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT id, title, assignee, status, block_kind, block_recurrences,
                       last_failure_error, last_heartbeat_at
                FROM tasks
                WHERE status = 'blocked'
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
            for row in rows:
                at, reason = latest_block_signal(conn, row["id"], row["last_heartbeat_at"])
                tasks.append(
                    BlockedTask(
                        board=board,
                        db_path=db_path,
                        task_id=str(row["id"]),
                        title=str(row["title"] or ""),
                        assignee=row["assignee"],
                        status=str(row["status"]),
                        block_kind=row["block_kind"],
                        block_recurrences=int(row["block_recurrences"] or 0),
                        last_failure_error=row["last_failure_error"],
                        latest_block_at=at,
                        latest_block_reason=reason,
                    )
                )
    return tasks


def is_provider_transient(conn: sqlite3.Connection, task: BlockedTask) -> bool:
    haystack = "\n".join(
        [
            str(task.block_kind or ""),
            task.latest_block_reason,
            task.last_failure_error or "",
            recent_comment_text(conn, task.task_id),
        ]
    ).casefold()
    if "provider-transient" in haystack or "provider transient" in haystack:
        return True
    return (task.block_kind or "").casefold() == "transient" and "provider" in haystack


def blocked_age_seconds(task: BlockedTask, now: int) -> int | None:
    if task.latest_block_at is None:
        return None
    return max(0, now - int(task.latest_block_at))


def unblock_task(conn: sqlite3.Connection, task_id: str, *, marker: str, reason: str) -> str:
    undone_parent = conn.execute(
        """
        SELECT 1
        FROM task_links l
        JOIN tasks p ON p.id = l.parent_id
        WHERE l.child_id = ? AND p.status != 'done'
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    new_status = "todo" if undone_parent else "ready"
    now = int(time.time())
    stale = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'blocked'",
        (task_id,),
    ).fetchone()
    if stale and stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, 'transient recovery on unblock'),
                   ended_at = ?, claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (now, int(stale["current_run_id"])),
        )
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = ?, current_run_id = NULL,
               consecutive_failures = 0, last_failure_error = NULL,
               claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
         WHERE id = ? AND status = 'blocked'
        """,
        (new_status, task_id),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"could not unblock {task_id}")
    append_event(conn, task_id, "unblocked", {"status": new_status, "marker": marker, "reason": reason})
    return new_status


def flip_to_needs_input(conn: sqlite3.Connection, task: BlockedTask, *, max_rounds: int) -> None:
    body = (
        f"{TRANSIENT_MARKER} escalated: transient auto-retry reached max_rounds={max_rounds}; "
        "flipping block_kind to needs_input for human/PM triage instead of retry-looping."
    )
    conn.execute(
        "UPDATE tasks SET block_kind = 'needs_input' WHERE id = ? AND status = 'blocked'",
        (task.task_id,),
    )
    add_comment(conn, task.task_id, body)
    append_event(conn, task.task_id, "transient_retry_escalated", {"max_rounds": max_rounds})


def run_cooldown(board_dir: Path, boards: Iterable[str] | None, cooldown_seconds: int, max_rounds: int, now: int) -> list[str]:
    lines: list[str] = []
    for task in discover_blocked(board_dir, boards):
        if (task.block_kind or "").casefold() != "transient":
            continue
        age = blocked_age_seconds(task, now)
        if age is None or age < cooldown_seconds:
            continue
        with connect(task.db_path) as conn:
            ensure_schema(conn)
            rounds = retry_comment_count(conn, task.task_id)
            with conn:
                if rounds >= max_rounds:
                    flip_to_needs_input(conn, task, max_rounds=max_rounds)
                    lines.append(f"ESCALATED {task.board}:{task.task_id} transient retries={rounds} -> needs_input")
                    continue
                next_round = rounds + 1
                add_comment(
                    conn,
                    task.task_id,
                    f"{TRANSIENT_MARKER} round {next_round}/{max_rounds}: block_kind=transient has been blocked for {age}s (cooldown={cooldown_seconds}s); auto-unblocking for retry.",
                )
                new_status = unblock_task(conn, task.task_id, marker=TRANSIENT_MARKER, reason="cooldown-elapsed")
                lines.append(f"UNBLOCKED {task.board}:{task.task_id} transient round={next_round}/{max_rounds} -> {new_status}")
    return lines


def run_provider_sweep(board_dir: Path, boards: Iterable[str] | None, now: int) -> list[str]:
    lines: list[str] = []
    for task in discover_blocked(board_dir, boards):
        with connect(task.db_path) as conn:
            ensure_schema(conn)
            if not is_provider_transient(conn, task):
                continue
            with conn:
                age = blocked_age_seconds(task, now)
                add_comment(
                    conn,
                    task.task_id,
                    f"{PROVIDER_MARKER}: upstream provider outage recovered; auto-unblocking provider-transient block for retry (blocked_age={age if age is not None else 'unknown'}s).",
                )
                new_status = unblock_task(conn, task.task_id, marker=PROVIDER_MARKER, reason="provider-recovered")
                lines.append(f"UNBLOCKED {task.board}:{task.task_id} provider-transient -> {new_status}")
    return lines


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("cooldown", "provider-sweep"), required=True)
    ap.add_argument("--board-dir", type=Path, default=DEFAULT_BOARD_DIR)
    ap.add_argument("--board", action="append", dest="boards", help="Limit to a board slug; repeatable")
    ap.add_argument("--cooldown-seconds", type=int, default=3600)
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--now", type=int, default=None, help="Test hook: epoch seconds")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    now = int(args.now if args.now is not None else time.time())
    if args.mode == "provider-sweep":
        lines = run_provider_sweep(args.board_dir, args.boards, now)
    else:
        lines = run_cooldown(args.board_dir, args.boards, args.cooldown_seconds, args.max_rounds, now)
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
