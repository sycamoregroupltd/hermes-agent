#!/usr/bin/env python3
"""CANONICAL SOURCE — do not edit profile-local copies.

Scan all kanban board databases for task_comments with non-integer
created_at values (e.g. literal '%s' from direct-SQL format-string bugs).

Exits 0 when no corruption found.
Exits 1 and prints affected rows when found.
Pass --fix to replace corrupt values with a placeholder timestamp.

This is a read-only watchdog by default.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from hermes_cli import kanban_db as kb

KANBAN_DIR = Path(os.environ.get("KANBAN_BOARDS_DIR", os.path.expanduser("~/.hermes/kanban/boards")))


def boards() -> list[Path]:
    if not KANBAN_DIR.exists():
        return []
    return sorted(
        db for db in KANBAN_DIR.glob("*/kanban.db") if not db.parent.name.startswith("_")
    )


def scan_board(db_path: Path, fix: bool = False) -> list[dict]:
    """Return list of corrupt-created_at comment rows.

    A corrupt created_at is any TEXT value that is NOT parseable as
    a plain integer -- in particular the literal string '%s'.
    """
    corrupt: list[dict] = []
    board = db_path.parent.name
    try:
        conn = kb.connect(db_path=db_path)
        conn.row_factory = sqlite3.Row
        # SQLite's typeof() returns 'integer' or 'text'
        rows = conn.execute(
            "SELECT id, task_id, author, created_at, substr(body, 1, 120) AS body_excerpt "
            "FROM task_comments WHERE typeof(created_at) = 'text'"
        ).fetchall()
        for r in rows:
            val = r["created_at"]
            if val is None:
                continue
            try:
                int(val)
                # Parseable as integer — accept even if stored as text
                continue
            except (ValueError, TypeError):
                pass  # genuinely corrupt text
            corrupt.append({
                "board": board,
                "id": r["id"],
                "task_id": r["task_id"],
                "author": r["author"],
                "created_at": val,
                "body_excerpt": r["body_excerpt"] or "",
            })
        if fix and corrupt:
            now = int(time.time())
            for c in corrupt:
                conn.execute(
                    "UPDATE task_comments SET created_at = ? WHERE id = ? AND task_id = ?",
                    (now, c["id"], c["task_id"]),
                )
            conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        pass  # missing task_comments table or locked DB
    return corrupt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan kanban boards for corrupt task_comments.created_at values"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Replace corrupt created_at values with the current Unix timestamp",
    )
    parser.add_argument(
        "--board",
        default=None,
        help="Scan only this board slug (default: all boards)",
    )
    args = parser.parse_args()

    found: list[dict] = []
    for db in boards():
        if args.board and db.parent.name != args.board:
            continue
        found.extend(scan_board(db, fix=args.fix))

    if not found:
        if args.fix:
            print("OK — no corrupt timestamps to fix")
        else:
            print("OK — no non-integer created_at values found across all boards")
        return 0

    print(f"WARNING — {len(found)} corrupt created_at value(s):")
    print()
    print(f"{'Board':<20} {'CommentId':<10} {'TaskId':<20} {'Author':<20} {'created_at':<15} Body")
    print("-" * 120)
    for c in found:
        print(
            f"{c['board']:<20} {c['id']:<10} {c['task_id']:<20} "
            f"{c['author']:<20} {c['created_at']!r:<15} {c['body_excerpt']}"
        )

    if args.fix:
        print(f"\nFixed {len(found)} corrupt rows with current Unix timestamp.")
    else:
        print(f"\nRun with --fix to replace corrupt timestamps with current Unix time.")
        print("Note: fixing timestamps is safe for board operation (safe_int handles")
        print("corrupt values gracefully), but the producer bug should also be addressed.")

    return 1


if __name__ == "__main__":
    sys.exit(main())
