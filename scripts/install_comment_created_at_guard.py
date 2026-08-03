#!/usr/bin/env python3
"""
Install a SQLite BEFORE INSERT trigger on task_comments for every kanban board.
The trigger rejects any INSERT that puts a TEXT (non-integer) value into
created_at — the exact mechanism that wrote literal '%s' into the column.

This is the regression guard: it makes the '%s' bug PHYSICALLY IMPOSSIBLE
at the database level, regardless of what the application layer does.

To remove: sqlite3 <board>/kanban.db "DROP TRIGGER IF EXISTS guard_comment_created_at_integer;"
"""
import os
import sqlite3
import sys
import time
from pathlib import Path
from hermes_cli import kanban_db as kb

KANBAN_DIR = Path(os.environ.get("KANBAN_BOARDS_DIR", os.path.expanduser("~/.hermes/kanban/boards")))

TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS guard_comment_created_at_integer
BEFORE INSERT ON task_comments
WHEN typeof(NEW.created_at) != 'integer'
BEGIN
    SELECT RAISE(ABORT, 'task_comments.created_at must be integer type');
END;
"""

def install_guard(db_path: Path, dry_run: bool = False) -> bool:
    """Install the integer-created_at trigger. Returns True if installed/changed."""
    board = db_path.parent.name
    try:
        conn = kb.connect(db_path=db_path)
        # Check if trigger already exists
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='guard_comment_created_at_integer'"
        ).fetchone()

        if existing:
            print(f"  ✓ {board}: guard already installed")
            conn.close()
            return False

        if dry_run:
            print(f"  ~ {board}: would install trigger (dry-run)")
            conn.close()
            return True

        conn.execute(TRIGGER_SQL)
        conn.commit()
        # Verify it was installed
        verify = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='guard_comment_created_at_integer'"
        ).fetchone()
        conn.close()

        if verify:
            print(f"  ✓ {board}: trigger installed successfully")
            return True
        else:
            print(f"  ✗ {board}: trigger installation failed (unknown reason)")
            return False

    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            print(f"  - {board}: no task_comments table, skipping")
        else:
            print(f"  ! {board}: OperationalError: {e}")
        return False
    except Exception as e:
        print(f"  ! {board}: {type(e).__name__}: {e}")
        return False


def verify_guard_works(db_path: Path) -> bool:
    """Prove the trigger actually blocks text created_at."""
    board = db_path.parent.name
    try:
        conn = kb.connect(db_path=db_path)
        # Try to insert a row with text created_at — should fail
        try:
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                ("__test_verify__", "__guard_test__", "Regression guard verification — can be deleted", "%s"),
            )
            conn.commit()
            conn.execute("DELETE FROM task_comments WHERE task_id='__test_verify__'")
            conn.commit()
            conn.close()
            print(f"  ⚠ {board}: trigger DID NOT block text created_at — regression guard MISSING!")
            return False
        except (sqlite3.IntegrityError, sqlite3.OperationalError) as e:
            print(f"  ✓ {board}: trigger correctly blocked text created_at")
            conn.close()
            return True
    except Exception as e:
        print(f"  ! {board}: {type(e).__name__}: {e}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Install regression guard: SQLite trigger rejecting non-integer task_comments.created_at"
    )
    parser.add_argument("--board", default=None, help="Target a single board (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without changing anything")
    parser.add_argument("--verify", action="store_true", help="Verify trigger blocks text created_at")
    args = parser.parse_args()

    boards = sorted(
        db for db in KANBAN_DIR.glob("*/kanban.db")
        if not db.parent.name.startswith("_")
    )
    if args.board:
        boards = [b for b in boards if b.parent.name == args.board]
        if not boards:
            print(f"Board '{args.board}' not found")
            sys.exit(1)

    print(f"Installing regression guard on {len(boards)} board(s)...")
    print()

    installed = []
    for db in boards:
        changed = install_guard(db, dry_run=args.dry_run)
        if changed:
            installed.append(db.parent.name)

    print()
    print(f"Summary: {len(installed)}/{len(boards)} boards need trigger installation")

    if args.verify:
        print()
        print("Verifying triggers block text created_at...")
        all_pass = True
        for db in boards:
            if not verify_guard_works(db):
                all_pass = False
        if all_pass:
            print()
            print("✓ All triggers verified — regression guard is working")
        else:
            print()
            print("⚠ Some triggers not working — investigate")
            sys.exit(1)

    if installed:
        print()
        print(f"Installed on: {', '.join(installed)}")


if __name__ == "__main__":
    main()
