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
# 2026-08-29 (t_b2474c19, fable-db-architect): coverage + trustworthy signal.
#     * BOARDS widened from 5 to 10 by an explicit, applied inclusion rule:
#       protect a board iff its kanban.db holds >=1 task AND its slug is not a
#       demonstrable test/scaffold artifact (TEST_SLUG_RE). Added ai-restaurant,
#       ecohome, legacy-yss, orchestrator-sync, quicknote.
#     * BACKUP COVERAGE AND RESTORE ELIGIBILITY ARE NOW SEPARATE LISTS.
#       RESTORE_ELIGIBLE_BOARDS stays the 5 incumbents. Auto-restore overwrites
#       LIVE data; a board must not inherit that power merely by being added to
#       the backup set. A non-eligible board that fails integrity gets
#       snapshot + operator flag + alert + non-zero exit, and NO auto action.
#     * THE ARTIFACT IS NOW ASSERTED, NOT ASSUMED (memory
#       backup-watchdog-existence-vs-integrity). Every copy is re-opened
#       read-only and must pass: exists, >= MIN_BACKUP_BYTES and >= half the
#       source, integrity_check ok, readable tasks table, and a task count
#       inside the [live_before, live_after] window measured around the copy.
#       A copy that fails is DELETED (fail-closed) so it can never become a
#       restore candidate, and it forces a non-zero exit -- for a --no-agent
#       cron the exit code is the ONLY signal (memory
#       noagent-cron-exit-code-is-only-liveness).
#     * The per-board RESTORE store now receives only VERIFIED bytes. It
#       previously received the copy unconditionally.
#     * Copies use sqlite3 backup() from a READ-ONLY source (WAL-safe,
#       fail-closed), mirroring hermes_cli/backup.py::_safe_copy_db, instead of
#       shutil.copy2 of the live main file which can omit committed WAL pages.
#     * All read paths open mode=ro. NEVER immutable=1 -- that ignores the WAL
#       and yields a stale read (memory sqlite-immutable-ignores-wal).
#     * COVERAGE-GAP DETECTOR: every run re-applies the inclusion rule to every
#       board on disk. A qualifying board that is not in BOARDS exits 3, so the
#       hardcoded dict can never silently lag reality again.
#     * MANIFEST.json per run + a LATEST marker written ONLY after every
#       protected board verifies. Consumers: the next run's staleness assertion
#       (printed at start) and the fleet vault note Operations/
#       kanban-board-backup-coverage-and-verification.md.
"""Kanban DB integrity backup + gated auto-restore for the protected boards.

No-agent cron semantics: stdout is NEVER parsed -- only the exit code is the
signal. Runs PRAGMA integrity_check (and foreign_key_check) on each protected
board's kanban.db, takes a WAL-safe snapshot, then VERIFIES the resulting
artifact (integrity + task count vs live) before it is allowed to become a
restore candidate.

Exit codes:
  0  every protected board passed integrity AND its artifact verified, and no
     unprotected board qualifies for protection
  2  a board failed integrity_check/foreign_key_check, or a backup artifact
     failed verification (fail-closed: the bad artifact is deleted)
  3  backups are all fine, but a board on disk qualifies under the inclusion
     rule and is NOT in BOARDS (coverage gap -- edit BOARDS)

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

# --- artifact verification thresholds ----------------------------------------
# A backup is only a backup once the ARTIFACT is proven. These are the floors
# the artifact must clear before it is allowed into the restore store.
MIN_BACKUP_BYTES = int(os.environ.get("KANBAN_MIN_BACKUP_BYTES", "8192"))
# Tasks can be created/completed between the two live reads that bracket the
# copy, so the artifact count is checked against that WINDOW, not a point.
BACKUP_TASK_DELTA_TOLERANCE = int(os.environ.get("KANBAN_BACKUP_TASK_DELTA", "2"))
BACKUP_COPY_TIMEOUT_S = float(os.environ.get("KANBAN_BACKUP_COPY_TIMEOUT_S", "60"))
# Coverage inclusion rule: a board qualifies for protection at >= this many tasks.
COVERAGE_MIN_TASKS = int(os.environ.get("KANBAN_COVERAGE_MIN_TASKS", "1"))
# Global dated-backup retention. 0 == DISABLED (default): deleting backup
# artifacts is an irreversible data op and stays a Frank/operator decision.
# The run always REPORTS the store size so the growth is never invisible.
GLOBAL_BACKUP_KEEP = int(os.environ.get("KANBAN_GLOBAL_BACKUP_KEEP", "0"))
# Age at which the previous run's LATEST marker is called stale (cron is 60m).
LATEST_STALE_HOURS = float(os.environ.get("KANBAN_LATEST_STALE_HOURS", "3"))

LIVE_BOARDS_ROOT = "/home/frank/.hermes/kanban/boards"

# --- BACKUP COVERAGE ----------------------------------------------------------
# INCLUSION RULE (applied, not asserted -- see coverage_gap_report()):
#   protect a board iff  kanban.db holds >= COVERAGE_MIN_TASKS tasks
#                   AND  the slug is not a demonstrable test/scaffold artifact.
# Measured 2026-08-28T23:27Z, this rule selects exactly these 10 and rejects
# _archived/default/e2e-*/syco-trading/t_b92944dd (0 tasks) and
# dedupecheck/dedupesmoke*/skilldedupe/model-trial/testproj (test slugs).
BOARDS = {
    # --- incumbents (protected since 2026-07-28) ---
    "jarvis-os": "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db",
    "sycode-trading": "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db",
    "sycode-ai": "/home/frank/.hermes/kanban/boards/sycode-ai/kanban.db",
    "upero": "/home/frank/.hermes/kanban/boards/upero/kanban.db",
    "yorkstone-supplies": "/home/frank/.hermes/kanban/boards/yorkstone-supplies/kanban.db",
    # --- added 2026-08-29 (t_b2474c19) by the inclusion rule above ---
    "ai-restaurant": "/home/frank/.hermes/kanban/boards/ai-restaurant/kanban.db",
    "ecohome": "/home/frank/.hermes/kanban/boards/ecohome/kanban.db",
    "legacy-yss": "/home/frank/.hermes/kanban/boards/legacy-yss/kanban.db",
    "orchestrator-sync": "/home/frank/.hermes/kanban/boards/orchestrator-sync/kanban.db",
    "quicknote": "/home/frank/.hermes/kanban/boards/quicknote/kanban.db",
}

# --- RESTORE ELIGIBILITY (DELIBERATELY *NOT* THE SAME LIST AS BOARDS) ---------
# Auto-restore overwrites a LIVE board with backup bytes and destroys whatever
# in-flight work is newer than the backup. Backup coverage is cheap and
# reversible; restore authority is neither. A board earns restore authority by
# being OBSERVED backing up cleanly for a while, not by being added to BOARDS.
# Enrolling a board here is a reviewed change, same as activating auto-restore
# at all (KANBAN_AUTO_RESTORE_ENABLED, still default OFF).
RESTORE_ELIGIBLE_BOARDS = {
    b.strip()
    for b in os.environ.get(
        "KANBAN_RESTORE_ELIGIBLE_BOARDS",
        "jarvis-os,sycode-trading,sycode-ai,upero,yorkstone-supplies",
    ).split(",")
    if b.strip()
}

# Slugs that are demonstrable test / scaffolding / snapshot artifacts rather
# than boards anyone would want restored. Backing these up buys nothing and
# adds noise; each pattern below is matched against a real slug on disk.
TEST_SLUG_RE = re.compile(
    r"^[._]"                 # _archived, .bak_*, .quarantine-stubs
    r"|^default$"            # scaffolding board, 0 tasks
    r"|^e2e[-_]"             # e2e-t_dc22ee7e
    r"|^t[-_][0-9a-f]{6,}"   # t_b92944dd, t-<hex>-test harness boards
    r"|smoke\d*$"            # dedupesmoke154829
    r"|^dedupe"              # dedupecheck, dedupesmoke*
    r"|^skilldedupe"         # skill-dedupe exercise board
    r"|^model-trial$"
    r"|^testproj$"
    r"|^test[-_]"
    r"|^zz"                  # zzselftest convention
    r"|^syco-trading$"       # 0-task typo twin of sycode-trading
    r"|^reclaimtest"
    r"|^sla-test",
    re.I,
)

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

BOARDS_ROOT = TEST_ROOT if TEST_ROOT else LIVE_BOARDS_ROOT
LATEST_MARKER = os.path.join(BACKUP_ROOT, "LATEST")

if TEST_ROOT:
    BOARDS = {name: os.path.join(TEST_ROOT, name, "kanban.db") for name in BOARDS}


class BackupCopyTimeout(Exception):
    """sqlite3 backup() could not make progress because the source stayed locked."""


def _connect_ro(path, timeout=15):
    """Open a board/backup DB READ-ONLY.

    NEVER uses immutable=1: that flag makes SQLite ignore the -wal, which
    returns a stale read and has already caused one false-alert flood in this
    fleet (memory sqlite-immutable-ignores-wal). mode=ro reads the WAL.

    Falls back to a normal connection ONLY when the read-only open genuinely
    cannot proceed (a -wal needing recovery with no -shm and no writer
    attached). Without that fallback a healthy board would be misreported as
    corrupt, which is a worse failure than the write handle we are avoiding.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
        conn.execute("SELECT 1").fetchone()  # force the open/WAL path now, not later
        return conn
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(k in msg for k in ("unable to open", "readonly", "recovery", "attempt to write")):
            raise
        print(f"  note: read-only open of {os.path.basename(path)} failed ({exc}); retrying read-write")
        return sqlite3.connect(path, timeout=timeout)


def task_count(path):
    """Task count from a DB, read-only. None when it cannot be read at all."""
    try:
        conn = _connect_ro(path)
        try:
            return int(conn.execute("SELECT count(*) FROM tasks").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return None


def safe_copy_db(src, dst, timeout_seconds=BACKUP_COPY_TIMEOUT_S):
    """WAL-safe snapshot of a live SQLite DB via the online backup() API.

    Mirrors the fleet-native hermes_cli/backup.py::_safe_copy_db. The source is
    opened READ-ONLY, so this can never mutate a live board. Fail-closed: a
    partial destination is deleted rather than left behind, because
    find_latest_ok_backup() would otherwise be free to choose it.

    shutil.copy2 of the live main file (what this script used before) can omit
    committed WAL pages and yields a silently-behind backup.

    pages=-1 (copy every page inside ONE read transaction) is deliberate. A
    CHUNKED backup is restarted from scratch by SQLite whenever the source is
    written mid-copy, and jarvis-os / sycode-trading are written every few
    seconds -- chunking risks a copy that never converges. In WAL mode a read
    transaction does not block writers, so the single-step form is both safe
    for the live board and guaranteed self-consistent.
    """
    conn = bconn = None
    started = time.monotonic()
    try:
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=timeout_seconds)
        bconn = sqlite3.connect(dst)
        conn.backup(bconn, pages=-1)
        bconn.close()
        bconn = None
        conn.close()
        conn = None
        # backup() writes every page into the destination MAIN file, so the
        # destination -wal is empty and the -shm is pure derived state. Drop
        # both so the artifact is one self-contained file: sidecars beside
        # backups have been leaking here for months (226 orphans in the
        # sycode-ai store) and they confuse retention accounting.
        stale_wal = f"{dst}-wal"
        try:
            if os.path.getsize(stale_wal) == 0:
                os.unlink(stale_wal)
        except OSError:
            pass
        try:
            os.unlink(f"{dst}-shm")
        except OSError:
            pass
        return True, f"ok in {time.monotonic() - started:.1f}s"
    except Exception as exc:
        if bconn is not None:
            try:
                bconn.close()
            except Exception:
                pass
            bconn = None
        try:
            os.unlink(dst)
        except OSError:
            pass
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        for c in (bconn, conn):
            if c is not None:
                try:
                    c.close()
                except Exception:
                    pass


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
        # READ-ONLY: a backup watchdog must never hold a write handle on a live
        # board. Both PRAGMAs below are read paths.
        conn = _connect_ro(path, timeout=10)
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
        # READ-ONLY: opening backups read-write left orphan -wal/-shm sidecars
        # littered beside every artifact (226 of them in the sycode-ai store on
        # 2026-08-28) and mutates the very bytes we are preserving.
        conn = _connect_ro(path, timeout=10)
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
        conn = _connect_ro(path, timeout=10)
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
        entries = os.listdir(pb_dir)
    except OSError:
        return
    files = sorted(
        f for f in entries
        if f.startswith("kanban.db.backup.") and f.endswith(".sqlite3")
    )
    for f in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(os.path.join(pb_dir, f))
        except OSError:
            pass
    # Sweep sidecars orphaned by pruning. A -shm is always derived, and an
    # EMPTY -wal holds nothing; both are useless without their .sqlite3. A
    # NON-empty orphan -wal is left alone and reported -- it might hold pages.
    live = {f for f in files[-keep:]} if len(files) > keep else set(files)
    stranded = []
    for f in entries:
        for suffix in ("-shm", "-wal"):
            if not f.endswith(suffix):
                continue
            base = f[: -len(suffix)]
            if base in live:
                continue
            full = os.path.join(pb_dir, f)
            try:
                if suffix == "-wal" and os.path.getsize(full) > 0:
                    stranded.append(f)
                    continue
                os.remove(full)
            except OSError:
                pass
    return stranded


def _discard_artifact(path):
    """Delete a failed backup artifact. FAIL-CLOSED: an unverified file left on
    disk is a file find_latest_ok_backup() is free to restore from."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def backup_and_verify_board(name, path):
    """Snapshot the board, then PROVE the artifact. Returns (ok, record).

    A job exiting 0 is not a backup; only a verified artifact is. Per protected
    board this asserts, on the copy itself:
      * it exists;
      * it is non-trivial (>= MIN_BACKUP_BYTES and >= half the source) --
        catches the truncated/0-byte class;
      * PRAGMA integrity_check == ok, read back from the artifact;
      * it has a readable tasks table;
      * its task count sits inside the [live_before, live_after] window
        measured immediately either side of the copy (+/- tolerance), so a
        stale or empty artifact cannot pass.

    Only after all of that does the artifact enter the per-board RESTORE store.
    The previous version copied into the restore store unconditionally, so an
    unverified copy could become the source of a future auto-restore.
    """
    rec = {"board": name, "verified": False, "detail": ""}
    backup_dir = os.path.join(BACKUP_ROOT, date_str)
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f"{name}.kanban.db")
    rec["artifact"] = dst

    try:
        src_bytes = os.path.getsize(path)
    except OSError as exc:
        rec["detail"] = f"source unreadable: {exc}"
        return False, rec
    rec["source_bytes"] = src_bytes

    live_before = task_count(path)
    copied, why = safe_copy_db(path, dst)
    live_after = task_count(path)
    rec["live_before"] = live_before
    rec["live_after"] = live_after

    if not copied:
        rec["detail"] = f"snapshot copy failed: {why}"
        return False, rec
    if not os.path.isfile(dst):
        rec["detail"] = "artifact missing after copy"
        return False, rec

    size = os.path.getsize(dst)
    rec["bytes"] = size
    if size < MIN_BACKUP_BYTES or size * 2 < src_bytes:
        rec["detail"] = (
            f"artifact too small: {size}B (source {src_bytes}B, floor {MIN_BACKUP_BYTES}B)"
        )
        _discard_artifact(dst)
        return False, rec

    ok_integrity, detail_integrity = check_board(name, dst)
    rec["integrity"] = detail_integrity
    if not ok_integrity:
        rec["detail"] = f"artifact failed its own integrity check: {detail_integrity}"
        _discard_artifact(dst)
        return False, rec

    n_backup = task_count(dst)
    rec["backup_tasks"] = n_backup
    if n_backup is None:
        rec["detail"] = "artifact has no readable tasks table"
        _discard_artifact(dst)
        return False, rec
    if live_before is None or live_after is None:
        rec["detail"] = "live task count unreadable; artifact content cannot be verified"
        _discard_artifact(dst)
        return False, rec

    lo = min(live_before, live_after) - BACKUP_TASK_DELTA_TOLERANCE
    hi = max(live_before, live_after) + BACKUP_TASK_DELTA_TOLERANCE
    if not (lo <= n_backup <= hi):
        rec["detail"] = (
            f"task-count mismatch: artifact holds {n_backup}, live window "
            f"[{live_before},{live_after}] (tolerance {BACKUP_TASK_DELTA_TOLERANCE})"
        )
        _discard_artifact(dst)
        return False, rec

    try:
        rec["sha16"] = _sha16(dst)
    except OSError as exc:
        rec["detail"] = f"artifact unreadable for hashing: {exc}"
        _discard_artifact(dst)
        return False, rec

    # VERIFIED -- only now may it become a restore candidate.
    pb_dir = _per_board_backup_dir(path)
    os.makedirs(pb_dir, exist_ok=True)
    pb = os.path.join(pb_dir, f"kanban.db.backup.{date_str}.sqlite3")
    shutil.copy2(dst, pb)
    stranded = _prune_per_board_backups(pb_dir) or []
    if stranded:
        rec["stranded_wal"] = stranded
    rec["restore_store"] = pb
    rec["verified"] = True
    rec["detail"] = "ok"
    return True, rec


# --- coverage: the inclusion rule, re-applied against reality every run -------
def discover_boards(root):
    """Every board directory on disk that carries a kanban.db."""
    found = {}
    try:
        for slug in sorted(os.listdir(root)):
            db = os.path.join(root, slug, "kanban.db")
            if os.path.isfile(db):
                found[slug] = db
    except OSError:
        pass
    return found


def board_qualifies(slug, db_path):
    """THE inclusion rule. Returns (qualifies, reason)."""
    if TEST_SLUG_RE.search(slug):
        return False, "test/scaffold slug"
    n = task_count(db_path)
    if n is None:
        return False, "no readable tasks table"
    if n < COVERAGE_MIN_TASKS:
        return False, f"{n} tasks (< {COVERAGE_MIN_TASKS})"
    return True, f"{n} tasks"


def coverage_gap_report():
    """Boards that qualify for protection but are absent from BOARDS.

    This is the structural half of the fix: a hardcoded dict silently lags
    reality (16 of 21 boards were unprotected on 2026-08-29 and nothing said
    so). Re-deriving the rule every run means the dict can only ever be one
    cron tick behind, and the gap reaches the EXIT CODE.
    """
    gaps = []
    for slug, db in discover_boards(BOARDS_ROOT).items():
        if slug in BOARDS:
            continue
        ok, reason = board_qualifies(slug, db)
        if ok:
            gaps.append((slug, reason))
    return gaps


def _write_json_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _global_store_stats():
    """Cheap footprint report: run-dir count + newest run bytes. Deliberately
    does NOT du the whole tree every hour."""
    try:
        runs = sorted(d for d in os.listdir(BACKUP_ROOT)
                      if os.path.isdir(os.path.join(BACKUP_ROOT, d)))
    except OSError:
        return 0, 0
    newest_bytes = 0
    if runs:
        newest = os.path.join(BACKUP_ROOT, runs[-1])
        for f in os.listdir(newest):
            try:
                newest_bytes += os.path.getsize(os.path.join(newest, f))
            except OSError:
                pass
    return len(runs), newest_bytes


def _prune_global_backups(keep):
    """Retention for the global dated store. DISABLED by default (keep=0):
    deleting backups is an irreversible data op and belongs to the operator."""
    if keep <= 0:
        return []
    try:
        runs = sorted(d for d in os.listdir(BACKUP_ROOT)
                      if os.path.isdir(os.path.join(BACKUP_ROOT, d)))
    except OSError:
        return []
    removed = []
    for d in runs[:-keep] if len(runs) > keep else []:
        try:
            shutil.rmtree(os.path.join(BACKUP_ROOT, d))
            removed.append(d)
        except OSError:
            pass
    return removed


def _report_previous_latest():
    """Name the consumer of the LATEST marker: this line, at the next run."""
    try:
        with open(LATEST_MARKER, encoding="utf-8") as fh:
            prev = json.load(fh)
        age_h = (time.time() - float(prev.get("epoch") or 0)) / 3600.0
        state = "STALE" if age_h > LATEST_STALE_HOURS else "fresh"
        print(
            f"Previous verified run: {prev.get('run')} "
            f"({age_h:.1f}h ago, {state}; {prev.get('boards_verified')} boards verified)"
        )
    except FileNotFoundError:
        print("Previous verified run: none (no LATEST marker yet)")
    except Exception as exc:
        print(f"Previous verified run: LATEST marker unreadable ({exc})")


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


def _backup_health_path(board):
    return os.path.join(RESTORE_STATE_DIR, f"{board}.backup-health.json")


def _read_backup_health(board):
    try:
        with open(_backup_health_path(board), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_backup_health(board, ok, detail):
    """Persist per-board backup health and emit a one-shot RECOVERED signal.

    A monitor that only ever fires on failure leaves its alert standing after
    the condition clears -- the same hole a peer seat found in
    fleet-alert-card.sh, which closes a card only when the SAME alert re-fires.
    So: on the ok<-fail edge, say so once and retire the operator flag
    (renamed, never deleted -- the evidence of the incident survives).
    """
    prev = _read_backup_health(board)
    was_ok = prev.get("ok")
    try:
        _write_json_atomic(_backup_health_path(board), {
            "board": board, "ok": bool(ok), "detail": detail,
            "at": date_str, "ts": time.time(),
        })
    except OSError as exc:
        print(f"  note: could not persist backup health for {board}: {exc}")
    if ok and was_ok is False:
        print(f"  [RECOVERED] {board}: backup verifies again (previously: {prev.get('detail')})")
        send_critical_alert(
            "FLEET_KANBAN_BACKUP_RECOVERED",
            f"{board} backup verified again at {date_str}; prior failure: {prev.get('detail')}",
        )
        flag = _operator_flag_path(board)
        if os.path.exists(flag):
            try:
                os.replace(flag, f"{flag}.cleared-{date_str}")
                print(f"           operator flag retired -> {os.path.basename(flag)}.cleared-{date_str}")
            except OSError:
                pass


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
    print(f"Protected boards: {len(BOARDS)} ({', '.join(sorted(BOARDS))})")
    print(f"Restore-eligible: {len(RESTORE_ELIGIBLE_BOARDS)} "
          f"({', '.join(sorted(RESTORE_ELIGIBLE_BOARDS))}) "
          f"— deliberately a SUBSET of the backup set")
    print(f"Auto-restore: {'ENABLED' if AUTO_RESTORE_ENABLED else 'disabled (activation gated)'}")
    _report_previous_latest()
    print()

    all_ok = True
    backup_count = 0
    alerts = []
    records = []
    for name, path in BOARDS.items():
        ok, detail = check_board(name, path)
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")

        if ok:
            verified, rec = backup_and_verify_board(name, path)
            records.append(rec)
            if verified:
                backup_count += 1
                print(
                    f"           VERIFIED -> {rec['artifact']} "
                    f"({rec['bytes']}B sha={rec['sha16']} tasks={rec['backup_tasks']} "
                    f"live=[{rec['live_before']},{rec['live_after']}] integrity=ok)"
                )
                if rec.get("stranded_wal"):
                    print(f"           note: non-empty orphan -wal left in restore store: {rec['stranded_wal']}")
                record_backup_health(name, True, "verified")
            else:
                # An unverifiable artifact is a FAILED BACKUP. For a --no-agent
                # cron only the exit code is read, so this must move all_ok.
                all_ok = False
                alert = f"BACKUP-VERIFY-FAIL {name}: {rec['detail']}"
                alerts.append(alert)
                print(f"  !! {alert}")
                print("           artifact discarded (fail-closed: it must not become a restore candidate)")
                _file_operator_flag(name, alert)
                send_critical_alert(
                    "FLEET_KANBAN_BACKUP_VERIFY_FAIL",
                    f"{name} backup artifact failed verification at {date_str}: {rec['detail']}",
                )
                record_backup_health(name, False, rec["detail"])
        else:
            all_ok = False
            record_backup_health(name, False, f"integrity: {detail}")
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

            if name not in RESTORE_ELIGIBLE_BOARDS:
                # BACKUP COVERAGE != RESTORE AUTHORITY. A board added to BOARDS
                # gets its data preserved; it does NOT thereby gain the power to
                # overwrite its own live file from a backup before anyone has
                # watched that backup work. Detect + flag + alert, no auto action.
                msg = (
                    f"{name} is backed up but NOT restore-eligible; "
                    "no automatic recovery attempted (operator decision)"
                )
                print(f"  !! {msg}")
                _file_operator_flag(name, msg)
                send_critical_alert(
                    "FLEET_KANBAN_DB_CORRUPT_NO_AUTO_RESTORE",
                    f"{name} corrupt at {date_str} ({cls}): {detail}. "
                    "Board is backup-covered but not restore-eligible — manual recovery required.",
                )
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

    # --- manifest + LATEST marker --------------------------------------------
    backup_dir = os.path.join(BACKUP_ROOT, date_str)
    every_board_verified = all_ok and backup_count == len(BOARDS)
    manifest = {
        "run": date_str,
        "generated_at": now.isoformat(),
        "protected_boards": sorted(BOARDS),
        "restore_eligible_boards": sorted(RESTORE_ELIGIBLE_BOARDS),
        "auto_restore_enabled": AUTO_RESTORE_ENABLED,
        "boards_verified": backup_count,
        "boards_protected": len(BOARDS),
        "all_verified": every_board_verified,
        "records": records,
    }
    try:
        _write_json_atomic(os.path.join(backup_dir, "MANIFEST.json"), manifest)
        print(f"\nManifest -> {os.path.join(backup_dir, 'MANIFEST.json')}")
    except OSError as exc:
        all_ok = False
        every_board_verified = False
        print(f"\n!! manifest write failed: {exc}")

    # The LATEST marker is written ONLY after every protected board verified.
    # Memory backup-watchdog-existence-vs-integrity: never publish an ok marker
    # ahead of the verification that earns it.
    if every_board_verified:
        try:
            _write_json_atomic(LATEST_MARKER, {
                "run": date_str,
                "epoch": time.time(),
                "boards_verified": backup_count,
                "dir": backup_dir,
            })
            print(f"LATEST -> {date_str} (written only because all {backup_count} boards verified)")
        except OSError as exc:
            all_ok = False
            print(f"!! LATEST marker write failed: {exc}")
    else:
        print("LATEST NOT updated — verification did not pass for every protected board.")

    # --- footprint + retention ------------------------------------------------
    n_runs, newest_bytes = _global_store_stats()
    print(f"Global store: {n_runs} run dirs, newest run {newest_bytes / 1048576:.0f} MiB "
          f"(retention {'keep=' + str(GLOBAL_BACKUP_KEEP) if GLOBAL_BACKUP_KEEP > 0 else 'DISABLED — operator decision'})")
    removed = _prune_global_backups(GLOBAL_BACKUP_KEEP)
    if removed:
        print(f"Pruned {len(removed)} old run dirs: {removed[:3]}{'...' if len(removed) > 3 else ''}")

    # --- coverage gap ---------------------------------------------------------
    gaps = coverage_gap_report()
    if gaps:
        print("\n=== COVERAGE GAP ===")
        for slug, reason in gaps:
            print(f"  UNPROTECTED BOARD {slug}: qualifies under the inclusion rule ({reason}) but is not in BOARDS")
        print("FLEET_KANBAN_BACKUP_COVERAGE_GAP: add the board(s) above to BOARDS "
              "(and decide separately whether they are restore-eligible)")

    print(f"\nVerified {backup_count}/{len(BOARDS)} protected boards.")
    if not all_ok:
        print("\n=== FLEET KANBAN DB INTEGRITY ALERT ===")
        for a in alerts:
            print(f"  {a}")
        print("FLEET_KANBAN_DB_INTEGRITY_FAIL: a board failed integrity_check or its backup failed verification")
        return 2
    if gaps:
        return 3
    print("HEALTHY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
