#!/usr/bin/env python3
"""Kanban board health check: integrity verify + GC for all active boards.

Canonical source — do not edit profile-local copies.
Runs as no-agent cron: produces stdout summary delivered to channel.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path("/home/frank/.hermes")
BOARDS_DIR = HERMES_HOME / "kanban" / "boards"
HERMES_BIN = "/home/frank/.local/bin/hermes"

BOARDS = [
    "jarvis-os",
    "sycode-trading",
    "upero",
    "yorkstone-supplies",
    "orchestrator-sync",
    "default",
]

ACTIVE_BOARDS = [b for b in BOARDS if (BOARDS_DIR / b / "kanban.db").exists()]


def check_integrity(board: str) -> str | None:
    """Run PRAGMA integrity_check. Returns error message or None."""
    db = BOARDS_DIR / board / "kanban.db"
    if not db.exists():
        return f"MISSING: {board}/kanban.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        if result != "ok":
            return f"CORRUPT: {board} — {result}"
        return None
    except Exception as e:
        return f"ERROR: {board} — {e}"


def run_gc(board: str) -> str:
    """Run kanban GC and return summary."""
    try:
        cp = subprocess.run(
            [HERMES_BIN, "kanban", "--board", board, "gc"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        out = cp.stdout.strip()
        if cp.returncode != 0:
            return f"GC FAILED: {board} — {out[:200]}"
        return out
    except subprocess.TimeoutExpired:
        return f"GC TIMEOUT: {board}"
    except Exception as e:
        return f"GC ERROR: {board} — {e}"


def main() -> int:
    errors = 0

    print(f"Kanban Health Check — {len(ACTIVE_BOARDS)} boards")
    print(f"{'='*60}")

    # Phase 1: Integrity check
    print("\n[Phase 1] Integrity Check")
    for board in ACTIVE_BOARDS:
        err = check_integrity(board)
        if err:
            print(f"  ! {err}")
            errors += 1
        else:
            print(f"  ✓ {board}")

    # Phase 2: GC
    print("\n[Phase 2] Garbage Collection")
    for board in ACTIVE_BOARDS:
        result = run_gc(board)
        print(f"  {board}: {result}")

    print(f"\n{'='*60}")
    print(f"Result: {errors} error(s)")
    return errors if errors else 0


if __name__ == "__main__":
    sys.exit(main())
