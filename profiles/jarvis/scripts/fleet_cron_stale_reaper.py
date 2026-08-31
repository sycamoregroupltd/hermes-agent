#!/usr/bin/env python3
"""Fleet-wide out-of-process reaper for abandoned cron execution rows.

Option A decision: reclaim rows from every profile store independently of that
profile's gateway. A row is eligible only when it is claimed/running, started
at least two hours ago, unfinished, and its (pid,start-time) owner is provably
dead. Uncertainty is fail-safe: rows with unreadable /proc state are untouched.
This is deliberately no-agent compatible: stdout is empty when nothing changes.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_HOME = Path("/home/frank/.hermes")
MIN_AGE_HOURS = 2
ERROR = ("Execution owner process exited before a durable terminal state was "
         "written; side effects are unknown (fleet out-of-process stale-row reaper).")


def now() -> datetime:
    return datetime.now(timezone.utc)


def proc_start(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().split()[21])
    except (FileNotFoundError, PermissionError, OSError, IndexError, ValueError):
        return None


def owner_dead(pid: int, started: int | None) -> bool:
    # A missing process is provably dead. If it exists but its recorded start
    # fingerprint is absent/mismatched, do not risk rewriting a reused PID.
    live_start = proc_start(pid)
    if live_start is None:
        return True
    return started is not None and live_start != int(started)


def reap_db(db: Path, *, dry_run: bool = False, age_hours: float = MIN_AGE_HOURS) -> list[dict]:
    changed: list[dict] = []
    conn = sqlite3.connect(db, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cutoff = (now() - timedelta(hours=age_hours)).isoformat()
        rows = conn.execute(
            "SELECT id,job_id,source,process_id,pid,process_started_at,status "
            "FROM executions WHERE status IN ('claimed','running') "
            "AND finished_at IS NULL AND started_at IS NOT NULL "
            "AND datetime(started_at) < datetime(?)", (cutoff,)).fetchall()
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            if not owner_dead(int(row["pid"]), row["process_started_at"]):
                continue
            rec = dict(row)
            rec["db"] = str(db)
            if not dry_run:
                cur = conn.execute(
                    "UPDATE executions SET status='unknown',finished_at=?,error=? "
                    "WHERE id=? AND status IN ('claimed','running') AND finished_at IS NULL",
                    (now().isoformat(), ERROR, row["id"]))
                if cur.rowcount != 1:
                    continue
            changed.append(rec)
        if not dry_run:
            conn.commit()
            if changed:
                log = db.parent / "stale_terminalized.jsonl"
                with log.open("a", encoding="utf-8") as fh:
                    for rec in changed:
                        fh.write(json.dumps({"row": rec["id"], "job_id": rec["job_id"],
                                             "status": "unknown", "at": now().isoformat()}) + "\n")
    finally:
        conn.close()
    return changed


def scan(root: Path, *, dry_run: bool = False, age_hours: float = MIN_AGE_HOURS) -> list[dict]:
    out: list[dict] = []
    for db in sorted(root.glob("*/cron/executions.db")):
        try:
            out.extend(reap_db(db, dry_run=dry_run, age_hours=age_hours))
        except (OSError, sqlite3.Error):
            # One locked/corrupt store must not prevent other profiles being
            # repaired. The health probe remains the owner of alerting.
            continue
    return out


def self_test() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); db = root / "stopped/cron/executions.db"; db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE executions (id TEXT PRIMARY KEY,job_id TEXT,source TEXT,process_id TEXT,pid INTEGER,process_started_at INTEGER,status TEXT,claimed_at TEXT,started_at TEXT,finished_at TEXT,error TEXT)")
        old = (now() - timedelta(hours=3)).isoformat()
        conn.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("dead","job","direct","x",99999999,1,"running",old,old,None,None))
        conn.commit(); conn.close()
        assert len(scan(root)) == 1
        conn = sqlite3.connect(db); row = conn.execute("SELECT status,finished_at FROM executions WHERE id='dead'").fetchone(); conn.close()
        assert row[0] == "unknown" and row[1]
        assert scan(root) == []
        clean = root / "clean/cron/executions.db"; clean.parent.mkdir(parents=True)
        conn = sqlite3.connect(clean); conn.execute("CREATE TABLE executions (id TEXT PRIMARY KEY,job_id TEXT,source TEXT,process_id TEXT,pid INTEGER,process_started_at INTEGER,status TEXT,claimed_at TEXT,started_at TEXT,finished_at TEXT,error TEXT)"); conn.commit(); conn.close()
        assert scan(root) == []
    print("SELFTEST PASS: dead stopped-store row reaped; second run and clean store silent")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--self-test", action="store_true"); ap.add_argument("--root", type=Path, default=DEFAULT_HOME / "profiles"); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--age-hours", type=float, default=MIN_AGE_HOURS)
    args = ap.parse_args()
    if args.self_test: return self_test()
    changed = scan(args.root, dry_run=args.dry_run, age_hours=args.age_hours)
    for rec in changed:
        print(f"cron_stale_direct_rows reaped profile={Path(rec['db']).parents[1].name} row={rec['id']} status=unknown")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
