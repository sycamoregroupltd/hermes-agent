#!/usr/bin/env python3
"""Retention prune for the DEFAULT profile store: /home/frank/.hermes/state.db ONLY.

Policy: delete sessions (and their messages) whose COALESCE(ended_at, started_at)
is older than RETENTION_DAYS (90). Backup-rotate (keep last 2), transactional
delete, FTS handling, VACUUM, one-line result appended to the retention log.

Safety contract (abort-not-force):
  - NEVER touches /home/frank/.hermes/profiles/*/state.db.
  - Aborts if any process holds the DB (fuser), if -wal/-shm siblings exist
    with a holder, if free disk < MIN_FREE_BYTES, if schema is unexpected,
    or if the backup fails integrity_check. No process kills, ever.
  - All aborts are logged to the retention log and exit non-zero (fail visibly).

Consumers of /home/frank/.hermes/logs/state-db-retention.log:
  - fleet-optimization loop phase 5 disk sweep
  - system-optimizer profile

Usage: prune-default-state-db.py [--dry-run]
"""
import argparse
import datetime
import glob
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
import time

DB_PATH = "/home/frank/.hermes/state.db"
BACKUP_DIR = "/home/frank/.hermes/backups"
BACKUP_PREFIX = "state.db-retention-"
BACKUPS_TO_KEEP = 2
LOG_PATH = "/home/frank/.hermes/logs/state-db-retention.log"
RETENTION_DAYS = 90
MIN_FREE_BYTES = 3 * 1024**3  # 3 GB
REQUIRED_TABLES = {"sessions", "messages", "messages_fts", "messages_fts_trigram"}
FTS_DELETE_TRIGGERS = {"messages_fts_delete", "messages_fts_trigram_delete"}


def log_line(status, detail):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_PATH, "a") as f:
        f.write(f"{ts} {status} db={DB_PATH} {detail}\n")


def abort(reason):
    log_line("ABORT", reason)
    print(f"ABORT: {reason}", file=sys.stderr)
    sys.exit(1)


def preflight():
    # Scope guard: refuse to run against anything under profiles/.
    real = os.path.realpath(DB_PATH)
    if "/profiles/" in real or real != "/home/frank/.hermes/state.db":
        abort(f"scope-guard: resolved path {real} is not the default-profile store")
    if not os.path.exists(DB_PATH):
        abort("db missing")
    # Disk space on the filesystem holding the DB.
    if shutil.disk_usage(os.path.dirname(DB_PATH)).free < MIN_FREE_BYTES:
        abort("insufficient free disk (<3GB)")
    # No holders: fuser exit 0 means someone holds it -> abort, never force.
    for sib in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(sib):
            r = subprocess.run(["fuser", sib], capture_output=True, text=True)
            if r.returncode == 0:
                abort(f"{sib} held by pid(s):{r.stdout.strip()} — not forcing")
    # Schema sanity.
    con = sqlite3.connect(DB_PATH)
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}
    finally:
        con.close()
    missing = REQUIRED_TABLES - names
    if missing:
        abort(f"unexpected schema, missing tables: {sorted(missing)}")
    # Triggers present -> deletes cascade to FTS; absent -> rebuild needed.
    return FTS_DELETE_TRIGGERS.issubset(names)


def candidate_counts(con, cutoff):
    q_sessions = ("SELECT COUNT(*) FROM sessions "
                  "WHERE COALESCE(ended_at, started_at) < ?")
    q_messages = ("SELECT COUNT(*) FROM messages WHERE session_id IN "
                  "(SELECT id FROM sessions WHERE COALESCE(ended_at, started_at) < ?)")
    return (con.execute(q_sessions, (cutoff,)).fetchone()[0],
            con.execute(q_messages, (cutoff,)).fetchone()[0])


def rotate_backups():
    """Take a fresh .backup, verify integrity, gzip, keep newest BACKUPS_TO_KEEP."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    raw = os.path.join(BACKUP_DIR, f"{BACKUP_PREFIX}{stamp}")
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    chk = sqlite3.connect(raw)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        chk.close()
    if ok != "ok":
        os.remove(raw)
        abort(f"backup integrity_check failed: {ok}")
    gz = raw + ".gz"
    with open(raw, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout)
    os.remove(raw)
    kept = sorted(glob.glob(os.path.join(BACKUP_DIR, BACKUP_PREFIX + "*.gz")))
    for old in kept[:-BACKUPS_TO_KEEP]:
        os.remove(old)
    return gz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what WOULD be deleted; no writes at all")
    args = ap.parse_args()

    triggers_ok = preflight()
    cutoff = time.time() - RETENTION_DAYS * 86400
    size_before = os.path.getsize(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    try:
        n_sessions, n_messages = candidate_counts(con, cutoff)
    finally:
        con.close()

    if args.dry_run:
        msg = (f"DRY-RUN would_delete_sessions={n_sessions} "
               f"would_delete_messages={n_messages} "
               f"cutoff={datetime.datetime.fromtimestamp(cutoff, datetime.timezone.utc):%Y-%m-%d} "
               f"size={size_before} fts_triggers={'present' if triggers_ok else 'MISSING'}")
        log_line("DRY-RUN", msg)
        print(msg)
        return

    if n_sessions == 0:
        # Idempotent no-op: nothing past retention. Skip backup churn and VACUUM.
        msg = f"NOOP deleted_sessions=0 deleted_messages=0 size={size_before}"
        log_line("OK", msg)
        print(msg)
        return

    backup_gz = rotate_backups()

    con = sqlite3.connect(DB_PATH)
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "DELETE FROM messages WHERE session_id IN "
            "(SELECT id FROM sessions WHERE COALESCE(ended_at, started_at) < ?)",
            (cutoff,))
        deleted_messages = con.execute("SELECT changes()").fetchone()[0]
        con.execute(
            "DELETE FROM sessions WHERE COALESCE(ended_at, started_at) < ?",
            (cutoff,))
        deleted_sessions = con.execute("SELECT changes()").fetchone()[0]
        con.execute("COMMIT")
        if not triggers_ok:
            # FTS not trigger-synced: rebuild both indexes.
            con.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            con.execute(
                "INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild')")
        con.execute("VACUUM")
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        con.close()
        abort(f"prune failed, rolled back: {e!r}")
    con.close()

    size_after = os.path.getsize(DB_PATH)
    msg = (f"OK deleted_sessions={deleted_sessions} deleted_messages={deleted_messages} "
           f"size_before={size_before} size_after={size_after} backup={backup_gz}")
    log_line("OK", msg)
    print(msg)


if __name__ == "__main__":
    main()
