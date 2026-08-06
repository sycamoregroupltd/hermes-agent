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
# 2026-08-04 (t_ef96fa85, rebuilding t_2735bc3b implementation lost to an
#   uncommitted revert): auto-restore-on-corruption path added per the ACCEPTED
#   proposal t_419c5f2a and the binding SEAT DECISION constraint DO NOT
#   BLIND-RESTORE:
#     * classify_corruption() distinguishes index-only faults (lossless REINDEX)
#       from genuine page corruption (restore latest verified-ok backup) from
#       unknown classes (operator flag + hard alert, NO auto action).
#     * restore_board(): reentrancy guard (state json + operator flag), quiesce
#       dispatcher a9def8c365df around the swap, WAL checkpoint(TRUNCATE),
#       atomic copy+fsync+os.replace from a verified-ok backup, post-restore
#       integrity verify, resume dispatcher in finally.
#     * send_critical_alert() -> discord:#critical-alerts (whatsapp:Frank
#       failover), carrying board, timestamp, chosen backup, lost-task delta.
#     * ACTIVATION GATED: AUTO_RESTORE_ENABLED defaults OFF; production cron
#       behavior unchanged (detect + snapshot + alert + per-board backup) until
#       os-reviewer signs activation.
"""Kanban DB integrity backup + gated auto-restore for 5 operational boards.

No-agent cron semantics: runs sqlite3 PRAGMA integrity_check (and
foreign_key_check) on each board's kanban.db, copies the DB to dated backups
(global + per-board restore store) if healthy, and reports any failures to
stdout. Exits 0 only if all boards pass integrity_check + foreign_key_check.

When KANBAN_AUTO_RESTORE_ENABLED=1 (ACTIVATION GATED, default off), a failing
board is auto-recovered by class:
  - index class  -> lossless REINDEX (preserves every row)
  - page class   -> restore newest verified-ok backup (atomic, dispatcher
                    quiesced), reentrancy-guarded, with lost-task delta alert
  - unknown      -> operator flag + hard alert, NO auto action
KANBAN_DRY_RUN=1 and KANBAN_TEST_BOARDS_ROOT=<root> are test hooks: dry-run
skips real dispatcher pause/resume and real alert sends (prints [DRY-RUN] ...),
and the test root relocates all boards/backups/state under that root so live
boards are never touched.
"""
import datetime, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, time

now = datetime.datetime.now(datetime.timezone.utc)
date_str = now.strftime("%Y%m%dT%H%M%SZ")

# --- environment / activation gates ------------------------------------------
HERMES = os.environ.get("KANBAN_INTEGRITY_HERMES", "/home/frank/.local/bin/hermes")
DISPATCH_JOB = os.environ.get("KANBAN_DISPATCH_JOB", "a9def8c365df")
ALERT_TARGET = os.environ.get("KANBAN_ALERT_TARGET", "discord:#critical-alerts")
WA_FALLBACK = os.environ.get("KANBAN_WA_FALLBACK", "whatsapp:Frank")
TEST_ROOT = os.environ.get("KANBAN_TEST_BOARDS_ROOT") or None
# ACTIVATION GATED: production default is OFF. os-reviewer must sign activation
# before the cron may auto-restore; gated mode only detects + reports.
AUTO_RESTORE_ENABLED = os.environ.get("KANBAN_AUTO_RESTORE_ENABLED", "0") == "1"
DRY_RUN = os.environ.get("KANBAN_DRY_RUN", "0") == "1"
RESTORE_COOLDOWN_SECONDS = int(os.environ.get("KANBAN_RESTORE_COOLDOWN_SECONDS", "3600"))
PER_BOARD_BACKUP_KEEP = int(os.environ.get("KANBAN_PER_BOARD_BACKUP_KEEP", "48"))

BOARDS = {
    "jarvis-os": "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db",
    "sycode-trading": "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db",
    "sycode-ai": "/home/frank/.hermes/kanban/boards/sycode-ai/kanban.db",
    "upero": "/home/frank/.hermes/kanban/boards/upero/kanban.db",
    "yorkstone-supplies": "/home/frank/.hermes/kanban/boards/yorkstone-supplies/kanban.db",
}

BACKUP_ROOT = (
    os.path.join(TEST_ROOT, "backups", "integrity-check")
    if TEST_ROOT
    else "/home/frank/.hermes/kanban/backups/integrity-check"
)
RESTORE_STATE_DIR = (
    os.path.join(TEST_ROOT, "state", "kanban-restore")
    if TEST_ROOT
    else "/home/frank/.hermes/state/kanban-restore"
)

if TEST_ROOT:
    BOARDS = {name: os.path.join(TEST_ROOT, name, "kanban.db") for name in BOARDS}


def _sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def classify_corruption(detail):
    """Classify a check_board failure detail.

    Returns:
      - 'index'  -> index-only fault: safe to REINDEX (lossless)
      - 'page'   -> genuine page/row corruption: restore verified-ok backup
      - 'unknown'-> everything else (FK violations, locked db, missing file):
                    operator flag + hard alert, NO auto action
    """
    d = str(detail)
    if re.search(r"(wrong # of entries in index|missing from index|Row \d+ missing)", d, re.I):
        return "index"
    if re.search(r"(database disk image is malformed|file is not a database|page \d+ is corrupt|malformed)", d, re.I):
        return "page"
    return "unknown"


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


def _backup_is_ok(path):
    """A restore candidate is only usable if its OWN integrity_check passes."""
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            cur = conn.execute("PRAGMA integrity_check")
            return all(row[0] == "ok" for row in cur.fetchall())
        finally:
            conn.close()
    except Exception:
        return False


def _task_ids(path):
    """Read all task ids from a board DB. Returns a set (empty on failure)."""
    ids = set()
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            for (tid,) in conn.execute("SELECT id FROM tasks"):
                ids.add(str(tid))
        finally:
            conn.close()
    except Exception:
        pass
    return ids


def _per_board_backup_dir(path):
    """Per-board restore store: <board_root>/backups/integrity-check/."""
    return os.path.join(os.path.dirname(path), "backups", "integrity-check")


def _prune_per_board_backups(pb_dir, keep=PER_BOARD_BACKUP_KEEP):
    try:
        files = sorted(
            f for f in os.listdir(pb_dir)
            if f.startswith("kanban.db.backup.") and f.endswith(".sqlite3")
        )
    except OSError:
        return
    for f in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(pb_dir, f))
        except OSError:
            pass


def backup_board(name, path):
    """Copy the DB to global dated backups AND the per-board restore store."""
    backup_dir = os.path.join(BACKUP_ROOT, date_str)
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f"{name}.kanban.db")
    shutil.copy2(path, dst)
    # per-board restore store (what restore_board reads from)
    pb_dir = _per_board_backup_dir(path)
    os.makedirs(pb_dir, exist_ok=True)
    pb = os.path.join(pb_dir, f"kanban.db.backup.{date_str}.sqlite3")
    shutil.copy2(path, pb)
    _prune_per_board_backups(pb_dir)
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


def find_latest_ok_backup(board_name, live_db_path):
    """Newest verified-ok backup for a board; per-board store first, then
    legacy global dated backups. Only candidates whose own integrity_check
    passes are usable (runbook pitfall #3)."""
    pb_dir = _per_board_backup_dir(live_db_path)
    candidates = []
    if os.path.isdir(pb_dir):
        try:
            candidates += [
                os.path.join(pb_dir, f)
                for f in os.listdir(pb_dir)
                if f.startswith("kanban.db.backup.") and f.endswith(".sqlite3")
            ]
        except OSError:
            pass
    candidates.sort(reverse=True)  # ISO timestamps in the names sort newest-first
    for c in candidates:
        if _backup_is_ok(c):
            return c
    # legacy global fallback: <BACKUP_ROOT>/<date_str>/<board>.kanban.db
    if os.path.isdir(BACKUP_ROOT):
        try:
            for d in sorted(os.listdir(BACKUP_ROOT), reverse=True):
                p = os.path.join(BACKUP_ROOT, d, f"{board_name}.kanban.db")
                if os.path.isfile(p) and _backup_is_ok(p):
                    return p
        except OSError:
            pass
    return None


# --- restore state / operator flags ------------------------------------------
def _restore_state_path(board):
    return os.path.join(RESTORE_STATE_DIR, f"{board}.json")


def _operator_flag_path(board):
    return os.path.join(RESTORE_STATE_DIR, f"{board}.operator-flag")


def _read_restore_state(board):
    try:
        with open(_restore_state_path(board), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_restore_state(board, payload):
    os.makedirs(RESTORE_STATE_DIR, exist_ok=True)
    tmp = _restore_state_path(board) + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, _restore_state_path(board))


def _file_operator_flag(board, reason):
    os.makedirs(RESTORE_STATE_DIR, exist_ok=True)
    payload = {"board": board, "reason": reason, "at": date_str, "ts": time.time()}
    tmp = _operator_flag_path(board) + f".tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, _operator_flag_path(board))


# --- external side effects: dispatcher quiesce + critical alerts --------------
def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _pause_dispatcher():
    """Quiesce the fleet dispatcher loop before a DB swap. Returns paused:bool."""
    if DRY_RUN:
        print(f"  [DRY-RUN] dispatcher quiesce skipped (would pause {DISPATCH_JOB})")
        return True
    r = _run([HERMES, "cron", "pause", DISPATCH_JOB])
    print(f"  dispatcher pause rc={r.returncode} {r.stdout.strip()[:100]}")
    return r.returncode == 0


def _resume_dispatcher():
    if DRY_RUN:
        print(f"  [DRY-RUN] dispatcher resume skipped (would resume {DISPATCH_JOB})")
        return
    try:
        r = _run([HERMES, "cron", "resume", DISPATCH_JOB])
        print(f"  dispatcher resume rc={r.returncode} {r.stdout.strip()[:100]}")
    except Exception as e:
        print(f"  dispatcher resume failed: {e}")


def send_critical_alert(subject, body):
    """Send to discord:#critical-alerts with whatsapp:Frank failover."""
    if DRY_RUN:
        print(f"[DRY-RUN] alert: {subject} | {body}")
        return
    r = _run([HERMES, "send", "-q", "-t", ALERT_TARGET, "-s", subject, body])
    if r.returncode == 0:
        print(f"ALERT-SENT target={ALERT_TARGET} subject={subject}")
        return
    print(f"ALERT-FAILED target={ALERT_TARGET} rc={r.returncode} stderr={r.stderr.strip()[:160]}")
    wa = _run([HERMES, "send", "-q", "-t", WA_FALLBACK, "-s", f"\U0001f501 FAILOVER: {subject}", body])
    if wa.returncode == 0:
        print(f"ALERT-FAILOVER-OK target={WA_FALLBACK} subject={subject}")
    else:
        print(f"ALERT-FAILOVER-FAILED target={WA_FALLBACK} rc={wa.returncode}")


def _wal_checkpoint(path):
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
    except Exception as e:
        print(f"  wal_checkpoint(TRUNCATE) skipped on unreadable db: {e}")


# --- recovery actions ---------------------------------------------------------
def restore_board(board_name, live_db_path, backup_path, corrupt_db_path):
    """Restore a page-corrupt board from a verified-ok backup.

    Returns (ok, detail, lost_task_delta). Reentrancy-guarded: a board whose
    post-restore check still fails is NOT re-restored within the cooldown —
    an operator flag is filed instead (hard alert raised by the caller).
    """
    now_ts = time.time()
    state = _read_restore_state(board_name)
    last_ok = state.get("post_restore_ok", True)
    last_ts = float(state.get("last_restore_ts") or 0)
    if last_ok is False and (now_ts - last_ts) < RESTORE_COOLDOWN_SECONDS:
        reason = (
            f"restore-loop guard: prior restore at {state.get('last_restore_at')} "
            "still failing; operator intervention required (no re-restore)"
        )
        print(f"  !! {reason}")
        _file_operator_flag(board_name, reason)
        return False, reason, []

    lost = sorted(_task_ids(corrupt_db_path) - _task_ids(backup_path))
    paused = False
    try:
        paused = _pause_dispatcher()
        if not paused:
            msg = "dispatcher quiesce failed; restore aborted"
            print(f"  !! {msg}")
            return False, msg, lost
        _wal_checkpoint(live_db_path)
        # atomic swap: copy backup -> temp on the same filesystem, fsync, rename
        tmp = os.path.join(os.path.dirname(live_db_path), f".restore-tmp.{os.getpid()}.sqlite3")
        with open(backup_path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, live_db_path)
        print(f"  swapped {board_name} <- {backup_path}")
        ok, detail = check_board(board_name, live_db_path)
        if ok:
            _write_restore_state(board_name, {
                "last_restore_at": date_str,
                "last_restore_ts": now_ts,
                "post_restore_ok": True,
                "post_restore_detail": detail,
                "backup": backup_path,
                "lost_task_delta": lost,
            })
            return True, f"restored from {backup_path}", lost
        reason = f"post-restore integrity still failing: {detail}"
        print(f"  !! {reason}")
        _write_restore_state(board_name, {
            "last_restore_at": date_str,
            "last_restore_ts": now_ts,
            "post_restore_ok": False,
            "post_restore_detail": detail,
            "backup": backup_path,
        })
        _file_operator_flag(board_name, reason)
        send_critical_alert(
            "FLEET_KANBAN_DB_RESTORE_FAIL",
            f"{board_name} restore from {backup_path} FAILED post-verify at {date_str}: {detail}",
        )
        return False, reason, lost
    finally:
        if paused:
            _resume_dispatcher()


def reindex_board(board_name, db_path):
    """Lossless REINDEX for index-only faults. Returns (ok, detail)."""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("REINDEX")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        msg = f"reindex error: {e}"
        print(f"  !! {msg}")
        _file_operator_flag(board_name, msg)
        send_critical_alert("FLEET_KANBAN_DB_REINDEX_FAIL", f"{board_name} REINDEX failed at {date_str}: {e}")
        return False, msg
    ok, detail = check_board(board_name, db_path)
    if ok:
        print(f"  reindexed {board_name} losslessly")
        return True, "reindexed"
    msg = f"post-reindex integrity still failing: {detail}"
    print(f"  !! {msg}")
    _file_operator_flag(board_name, msg)
    send_critical_alert("FLEET_KANBAN_DB_REINDEX_FAIL", f"{board_name} REINDEX still failing at {date_str}: {detail}")
    return False, msg


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
            cls = classify_corruption(detail)
            print(f"       corruption class: {cls}")

            if not AUTO_RESTORE_ENABLED:
                # Activation gated: production default. Detect + report only.
                print("AUTO-RESTORE DISABLED (activation gated)")
                continue

            if cls == "index":
                recovered, _ = reindex_board(name, path)
                if recovered:
                    send_critical_alert(
                        "FLEET_KANBAN_DB_REINDEXED",
                        f"{name} index fault repaired losslessly at {date_str}",
                    )
                    print(f"  [REINDEXED] {name}")
            elif cls == "page":
                backup = find_latest_ok_backup(name, path)
                if not backup:
                    msg = f"no verified-ok backup for {name}; operator intervention required"
                    print(f"  !! {msg}")
                    _file_operator_flag(name, msg)
                    send_critical_alert(
                        "FLEET_KANBAN_DB_RESTORE_FAIL",
                        f"{name} corrupt at {date_str} but no verified-ok backup found: {detail}",
                    )
                    continue
                restored, detail2, lost = restore_board(
                    name, path, backup, snap if os.path.isfile(str(snap)) else path
                )
                if restored:
                    send_critical_alert(
                        "FLEET_KANBAN_DB_RESTORED",
                        f"{name} restored at {date_str} from {backup}; lost-task delta: {lost}",
                    )
                    print(f"  [RESTORED] {name} from {backup}; lost-task delta: {lost}")
                else:
                    print(f"  [RESTORE-FAILED] {name}: {detail2}")
            else:
                msg = f"unclassified corruption {name}: {detail}"
                print(f"  !! {msg}")
                _file_operator_flag(name, msg)
                send_critical_alert(
                    "FLEET_KANBAN_DB_INTEGRITY_UNCLASSIFIED",
                    f"{name} corruption unclassified at {date_str}: {detail}",
                )

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
