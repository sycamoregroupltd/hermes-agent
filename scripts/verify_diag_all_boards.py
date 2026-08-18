#!/usr/bin/env python3
"""Read-only: search ALL kanban board DBs for diag: idempotency keys."""
import sqlite3
import sys
from pathlib import Path

BOARDS_DIR = Path("/home/frank/.hermes/kanban/boards")
LEGACY = Path("/home/frank/.hermes/kanban.db")

def main():
    candidates = []
    if BOARDS_DIR.exists():
        for d in sorted(BOARDS_DIR.iterdir()):
            db = d / "kanban.db"
            if db.is_file():
                candidates.append((d.name, str(db)))
    if LEGACY.exists():
        candidates.append(("legacy-flat", str(LEGACY)))
    total = 0
    for slug, db in candidates:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT id, idempotency_key, status, priority, assignee, created_at "
                "FROM tasks WHERE idempotency_key LIKE 'diag:%' ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            n = con.execute("SELECT count(*) FROM tasks WHERE idempotency_key LIKE 'diag:%'").fetchone()[0]
            con.close()
        except Exception as e:
            print(f"{slug}: ERR {e}")
            continue
        if n:
            total += n
            print(f"--- {slug} ({db}) total diag rows: {n}")
            for r in rows:
                print("   ", r)
    if total == 0:
        print("NO diag: cards on ANY board")

if __name__ == "__main__":
    main()
