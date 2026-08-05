#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# 2026-07-23 Restored: the previous profile-local copy was overwritten with an
# Obsidian incident document (YAML frontmatter + markdown), causing SyntaxError.
# Simplified from the original: runs PRAGMA integrity_check + copies DB files.
# 2026-07-28 (t_8a7ff2ae): hardened per spec — renamed from quick_check era to a
#   trustworthy watchdog:
#     * Runs PRAGMA integrity_check (full, NOT quick_check — quick_check misses
#       this corruption class).
#     * ALSO runs PRAGMA foreign_key_check with foreign_keys ON.
#     * On ANY failure, snapshots the corrupt DB to
#       kanban.db.corrupt.<sha16>.bak and prints an explicit ALERT block.
#     * Exits non-zero on failure so #fleet-reports receives the alert.
"""Kanban DB integrity backup for 5 operational boards.

No-agent cron semantics: runs sqlite3 PRAGMA integrity_check (and
foreign_key_check) on each board's kanban.db, copies the DB to a dated backup
if healthy, and reports any failures to stdout. Exits 0 only if all boards
pass integrity_check + foreign_key_check.
"""
import datetime, hashlib, os, shutil, sqlite3, sys

now = datetime.datetime.now(datetime.timezone.utc)
date_str = now.strftime("%Y%m%dT%H%M%SZ")

BOARDS = {
    "jarvis-os": "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db",
    "sycode-trading": "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db",
    "sycode-ai": "/home/frank/.hermes/kanban/boards/sycode-ai/kanban.db",
    "upero": "/home/frank/.hermes/kanban/boards/upero/kanban.db",
    "yorkstone-supplies": "/home/frank/.hermes/kanban/boards/yorkstone-supplies/kanban.db",
}

BACKUP_ROOT = "/home/frank/.hermes/kanban/backups/integrity-check"


def _sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def check_board(name, path):
    """Run PRAGMA integrity_check + foreign_key_check. Returns (ok, detail)."""
    if not os.path.exists(path):
        return False, f"DB not found: {path}"
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            cur = conn.execute("PRAGMA integrity_check")
            result = cur.fetchall()
            if not all(row[0] == "ok" for row in result):
                return False, f"integrity_check failed: {result[:3]}"
            # foreign_key_check requires foreign_keys ON to be meaningful.
            conn.execute("PRAGMA foreign_keys=ON")
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                return False, f"foreign_key_check found {len(fk)} violation(s): {fk[:3]}"
            return True, "ok"
        finally:
            conn.close()
    except Exception as e:
        return False, f"sqlite error: {e}"


def backup_board(name, path):
    """Copy the DB to a dated backup directory."""
    backup_dir = os.path.join(BACKUP_ROOT, date_str)
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f"{name}.kanban.db")
    shutil.copy2(path, dst)
    return dst


def snapshot_corrupt(name, path):
    """Snapshot a corrupt DB to kanban.db.corrupt.<sha16>.bak next to it."""
    try:
        token = _sha16(path)
    except Exception:
        token = "unknown"
    dst = os.path.join(os.path.dirname(path), f"{os.path.basename(path)}.corrupt.{token}.bak")
    try:
        shutil.copy2(path, dst)
        return dst
    except Exception as e:
        return f"<snapshot failed: {e}>"


def main():
    print(f"Kanban integrity backup — {date_str}")
    print(f"Boards to check: {len(BOARDS)}\n")

    all_ok = True
    backup_count = 0
    alerts = []
    for name, path in BOARDS.items():
        ok, detail = check_board(name, path)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")

        if ok:
            dst = backup_board(name, path)
            backup_count += 1
            print(f"           backed up -> {dst}")
        else:
            all_ok = False
            snap = snapshot_corrupt(name, path)
            alert = f"CORRUPT BOARD {name}: {detail} | snapshot -> {snap}"
            alerts.append(alert)
            print(f"  !! {alert}")

    print(f"\nBacked up {backup_count}/{len(BOARDS)} boards cleanly.")
    if all_ok:
        print("HEALTHY")
        return 0
    else:
        print("\n=== FLEET KANBAN DB INTEGRITY ALERT ===")
        for a in alerts:
            print(f"  {a}")
        print("FLEET_KANBAN_DB_INTEGRITY_FAIL: one or more boards failed integrity_check")
        return 2


if __name__ == "__main__":
    sys.exit(main())
