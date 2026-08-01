#!/usr/bin/env python3
"""CI gate: fail if any silent-exit phantom block exists (t_74c6693e).

The dispatcher (hermes_cli/kanban_db.py detect_crashed_workers) force-trips an
immediate auto-block when a worker exits rc=0 without a terminal kanban call.
That path is the recurring false-block class. This gate makes the regression
visible in CI: any card across the tracked fleet boards with

    status='blocked' AND last_failure_error LIKE '%without calling kanban_complete%'

causes a non-zero exit (after printing which boards/counts are affected).

It intentionally does NOT mutate the board — reaping is done by
reap_silent_exit_blocks.py. The gate's job is to flag, not to fix, so a human
or the reaper cron sees the regression before it masks real stalls.

Usage (CI):
  python3 ci_no_silent_exit_blocks.py      # exit 1 if any phantom block exists
  python3 ci_no_silent_exit_blocks.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

BOARDS_ROOT = "/home/frank/.hermes/kanban/boards"
BOARDS = ["jarvis-os", "sycode-trading", "yorkstone-supplies", "upero"]
SILENT_EXIT_ERR = "worker exited cleanly (rc=0) without calling kanban_complete or kanban_block"


def scan() -> dict[str, int]:
    out: dict[str, int] = {}
    for b in BOARDS:
        db = os.path.join(BOARDS_ROOT, b, "kanban.db")
        if not os.path.exists(db):
            continue
        c = sqlite3.connect(db)
        n = c.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='blocked' "
            "AND last_failure_error LIKE ?",
            (f"%{SILENT_EXIT_ERR}%",),
        ).fetchone()[0]
        c.close()
        out[b] = n
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    args = ap.parse_args()

    counts = scan()
    total = sum(counts.values())

    if args.json:
        print(json.dumps({"total": total, "by_board": counts}, indent=2))

    if total == 0:
        if not args.json:
            print("CI gate PASS: zero silent-exit phantom blocks across "
                  f"{', '.join(BOARDS)}.")
        return 0

    if not args.json:
        print("CI gate FAIL: silent-exit phantom blocks present:")
        for b, n in counts.items():
            if n:
                print(f"  {b}: {n}")
        print("Run reap_silent_exit_blocks.py --apply (or investigate gate cards).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
