#!/usr/bin/env python3
"""Read-only verification of kanban.db diag-card schema + active diag cards."""
import json
import sqlite3
import sys
from pathlib import Path

DB = "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db"

def main():
    if not Path(DB).exists():
        print("DB MISSING:", DB)
        sys.exit(1)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = [r[1] for r in con.execute("PRAGMA table_info(tasks)")]
    print("tasks cols:", cols)
    print("idempotency_key in schema:", "idempotency_key" in cols)
    rows = con.execute(
        "SELECT id, idempotency_key, status, priority, assignee, created_at "
        "FROM tasks WHERE idempotency_key LIKE 'diag:%' AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 25"
    ).fetchall()
    print("active (non-archived) diag cards:", len(rows))
    for r in rows:
        print(dict(r))
    # Count archived diag cards too (history)
    arch = con.execute(
        "SELECT count(*) FROM tasks WHERE idempotency_key LIKE 'diag:%' AND status = 'archived'"
    ).fetchone()[0]
    print("archived diag cards:", arch)
    con.close()

if __name__ == "__main__":
    main()
