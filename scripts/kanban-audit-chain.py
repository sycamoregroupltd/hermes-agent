#!/usr/bin/env python3
"""kanban-audit-chain.py — hash-chain tamper-evident audit layer for the Hermes
kanban event log (board jarvis-os). Kanban card t_21781f08; concept adapted from
block/buzz's buzz-audit crate (decision: fleet vault Decisions/
2026-08-01-buzz-block-evaluation-no-integrate.md).

DESIGN (fixed)
  - The chain lives in a SEPARATE sidecar SQLite DB (default
    ~/.hermes/audit/kanban-chain.db). This script NEVER writes to, migrates,
    or locks the live kanban DB — all reads of it are read-only (mode=ro)
    short transactions against the WAL database.
  - Chain model: walk task_events in stable id order. For each new event:
        row_hash = SHA256( prev_hash_hex || canonical_json(event_row) )
    where canonical_json = json.dumps({col: value for ALL columns},
    sort_keys=True, separators=(',',':'), ensure_ascii=True).
    Genesis prev_hash = SHA256("hermes-kanban-chain-genesis-v1").
  - The source column list is recorded in chain_meta so schema drift is
    detectable (a changed column set changes canonical_json semantics).
  - append is idempotent: re-running never duplicates or re-hashes existing
    chained events. append is FAIL-OPEN (always exit 0, print ERROR lines) —
    it must never block anything. verify is FAIL-LOUD (exit 1 on any break).

HONEST LIMITATION
  Events tampered with BEFORE their first chaining are undetectable — the
  chain notarizes whatever the row said when first seen. The detection window
  equals the chain-append cadence (the cron interval running `append`).
  Deleting the sidecar chain DB destroys history; the sidecar itself is
  protected only by filesystem access + the fact that a truncated/rebuilt
  chain shows a fresh genesis_at in chain_meta.

MODES
  append  [--db PATH] [--chain PATH]
      Chain new events. Prints "CHAIN-APPEND: chained=N total=M skipped=K".
      Exit 0 even on errors (ERROR lines to stdout/stderr). Per-row hashing
      failures are skip-and-flag (F7): the row stays unchained and verify
      later flags it as forged-insert once the tip passes it.
  verify  [--db PATH] [--chain PATH] [--max-lag-minutes N]
      1) chain self-integrity (stored prev_hash linkage from genesis),
      2) recompute every chained event's hash from the CURRENT kanban DB:
         hash mismatch = mutation, missing event_id = deletion,
      3) freshness: lag between newest kanban event and newest chained event.
         F1: the freshness basis is the created_at OF THE max(id) ROW — never
         max(created_at) (the live board carries year-2033 sentinel
         created_at rows, e.g. ids 20785/21442, that would poison max()).
      4) forged inserts below the tip (F4): any kanban id <= chain tip that
         was never chained can only have been inserted after the fact —
         counted in forged_below_tip and treated as a break.
      Machine-parseable last line:
        CHAIN-VERIFY: status=OK|FAIL breaks=N first_break_seq=S \
            first_break_event_id=E first_break_kind=K lag_min=L unchained=U \
            forged_below_tip=F genesis_at=G max_seq=Q
      (genesis_at/max_seq surfaced for the monitor's continuity check, F5.)
      Exit 0 clean, 1 on any break or lag breach.
  selftest
      Red/green proof on scratch copies (never touches the live chain),
      exercising the SAME code paths (cmd_append/cmd_verify, no reimpl):
      1 green pristine, 2 red mutation, 3 red deletion,
      4 green lag WITH a year-2033 sentinel below the tip (F1),
      5 red lag-only breach, 6 red forged-below-tip insert (F4),
      7 genesis-change flag: a rebuilt chain surfaces a new genesis_at (F5).
      Nonzero exit unless ALL of the above behave as specified.

Zero-write guarantee for the live DB: connections use file:...?mode=ro and
this process never issues DML/DDL against it. stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

DEFAULT_DB = "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db"
DEFAULT_CHAIN = "/home/frank/.hermes/audit/kanban-chain.db"
EVENTS_TABLE = "task_events"
GENESIS_SEED = "hermes-kanban-chain-genesis-v1"
SCHEMA_VERSION = 1
BATCH = 2000


def genesis_hash() -> str:
    return hashlib.sha256(GENESIS_SEED.encode("utf-8")).hexdigest()


def open_ro(path: str) -> sqlite3.Connection:
    """Read-only connection to the (possibly hot, WAL) kanban DB."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def open_chain(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        """CREATE TABLE IF NOT EXISTS chain (
               seq        INTEGER PRIMARY KEY AUTOINCREMENT,
               event_id   INTEGER NOT NULL UNIQUE,
               row_hash   TEXT    NOT NULL,
               prev_hash  TEXT    NOT NULL,
               chained_at INTEGER NOT NULL
           )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS chain_meta (
               key   TEXT PRIMARY KEY,
               value TEXT NOT NULL
           )"""
    )
    # G1 (t_78c65b78): retention-aware deletion classification. The chain
    # notarizes src_task_id/src_created_at alongside the hash so a LATER
    # disappearance can be checked against the documented `hermes kanban gc`
    # policy (terminal-task events older than the retention window) instead of
    # being reported as tamper. Added by ALTER for pre-existing sidecars.
    have = {r[1] for r in con.execute("PRAGMA table_info(chain)")}
    if "src_task_id" not in have:
        con.execute("ALTER TABLE chain ADD COLUMN src_task_id TEXT")
    if "src_created_at" not in have:
        con.execute("ALTER TABLE chain ADD COLUMN src_created_at INTEGER")
    # Evidence-backed ledger of events proven to have been removed by a
    # legitimate GC sweep (see cmd_reconcile_gc). Rows here are expected-absent
    # and never counted as breaks.
    con.execute(
        """CREATE TABLE IF NOT EXISTS gc_pruned (
               event_id    INTEGER PRIMARY KEY,
               task_id     TEXT,
               created_at  INTEGER,
               task_status TEXT,
               evidence    TEXT NOT NULL,
               recorded_at INTEGER NOT NULL
           )"""
    )
    con.commit()
    return con


def event_columns(kanban: sqlite3.Connection) -> list[str]:
    cols = [r[1] for r in kanban.execute(f"PRAGMA table_info({EVENTS_TABLE})")]
    if not cols:
        raise RuntimeError(f"table {EVENTS_TABLE} not found or has no columns")
    return cols


def canonical_json(row: sqlite3.Row, cols: list[str]) -> str:
    d = {}
    for c in cols:
        v = row[c]
        if isinstance(v, bytes):  # defensive: TEXT column abused as blob
            v = {"__bytes_hex__": v.hex()}
        d[c] = v
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def row_hash(prev_hash_hex: str, cjson: str) -> str:
    return hashlib.sha256((prev_hash_hex + cjson).encode("utf-8")).hexdigest()


def meta_get(chain: sqlite3.Connection, key: str):
    r = chain.execute("SELECT value FROM chain_meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def meta_set(chain: sqlite3.Connection, key: str, value: str) -> None:
    chain.execute(
        "INSERT INTO chain_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def ensure_meta(chain: sqlite3.Connection, db_path: str, cols: list[str]) -> list[str] | None:
    """Record/verify chain metadata. Returns the RECORDED column list (which
    governs hashing) or None on first init (caller records current cols)."""
    stored = meta_get(chain, "source_columns")
    if stored is None:
        meta_set(chain, "schema_version", str(SCHEMA_VERSION))
        meta_set(chain, "source_columns", json.dumps(cols))
        meta_set(chain, "source_events_table", EVENTS_TABLE)
        meta_set(chain, "genesis_seed", GENESIS_SEED)
        meta_set(chain, "genesis_hash", genesis_hash())
        meta_set(chain, "source_db_path", os.path.abspath(db_path))
        meta_set(chain, "genesis_at", str(int(time.time())))
        chain.commit()
        return cols
    return json.loads(stored)


def chain_tip(chain: sqlite3.Connection):
    """(last_event_id, last_row_hash, chained_count)"""
    r = chain.execute(
        "SELECT event_id, row_hash FROM chain ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    n = chain.execute("SELECT count(*) FROM chain").fetchone()[0]
    if r is None:
        return None, genesis_hash(), n
    return r[0], r[1], n


# ---------------------------------------------------------------- append ----
def cmd_append(args) -> int:
    """FAIL-OPEN by design: always exits 0; errors are printed, never raised."""
    try:
        kanban = open_ro(args.db)
        chain = open_chain(args.chain)
        live_cols = event_columns(kanban)
        recorded_cols = ensure_meta(chain, args.db, live_cols)
        if recorded_cols != live_cols:
            # Schema drift: hashing under the OLD recorded list would silently
            # change meaning; refuse to extend the chain but stay exit-0.
            print(
                "ERROR: source schema drift — recorded columns "
                f"{recorded_cols} != live columns {live_cols}; append halted "
                "(chain unchanged). Operator decision required."
            )
            print("CHAIN-APPEND: chained=0 total=%d skipped=0" % chain_tip(chain)[2])
            return 0

        last_id, prev, total = chain_tip(chain)
        floor = last_id if last_id is not None else -1
        chained = 0
        skipped = 0
        colsql = ", ".join(f'"{c}"' for c in recorded_cols)
        while True:
            # tiny read transaction per batch — never hold the live DB open
            rows = kanban.execute(
                f"SELECT {colsql} FROM {EVENTS_TABLE} WHERE id > ? "
                f"ORDER BY id LIMIT {BATCH}",
                (floor,),
            ).fetchall()
            if not rows:
                break
            now = int(time.time())
            batch = []
            for r in rows:
                rid = r["id"]
                try:  # F7: per-row skip-and-flag — one bad row never aborts the walk
                    h = row_hash(prev, canonical_json(r, recorded_cols))
                except Exception as exc:
                    skipped += 1
                    print(
                        f"ERROR: append skipped event_id={rid}: "
                        f"{type(exc).__name__}: {exc} (row left unchained; "
                        "verify flags it as forged-insert once the tip passes it)"
                    )
                    floor = rid
                    continue
                batch.append((rid, h, prev, now,
                              r["task_id"] if "task_id" in recorded_cols else None,
                              r["created_at"] if "created_at" in recorded_cols else None))
                prev = h
                floor = rid
            if batch:
                chain.executemany(
                    "INSERT INTO chain(event_id,row_hash,prev_hash,chained_at,"
                    "src_task_id,src_created_at) VALUES(?,?,?,?,?,?)",
                    batch,
                )
                chain.commit()
                chained += len(batch)
        print(f"CHAIN-APPEND: chained={chained} total={total + chained} skipped={skipped}")
        return 0
    except Exception as exc:  # never block anything
        print(f"ERROR: append failed: {type(exc).__name__}: {exc}")
        print("CHAIN-APPEND: chained=0 total=unknown skipped=unknown")
        return 0


# ---------------------------------------------------------------- verify ----
def cmd_verify(args) -> int:
    breaks = 0
    first = None  # (seq, event_id, kind)

    def record(seq, event_id, kind, detail=""):
        nonlocal breaks, first
        breaks += 1
        if first is None:
            first = (seq, event_id, kind)
        if breaks <= 20:
            print(f"BREAK seq={seq} event_id={event_id} kind={kind} {detail}")
        elif breaks == 21:
            print("... further breaks suppressed ...")

    try:
        kanban = open_ro(args.db)
        chain = open_chain(args.chain)
    except Exception as exc:
        print(f"ERROR: cannot open databases: {type(exc).__name__}: {exc}")
        print(
            "CHAIN-VERIFY: status=FAIL breaks=1 first_break_seq=- "
            "first_break_event_id=- first_break_kind=open-error lag_min=- unchained=- "
            "forged_below_tip=- genesis_at=- max_seq=-"
        )
        return 1

    recorded_cols_raw = meta_get(chain, "source_columns")
    if recorded_cols_raw is None:
        print("ERROR: chain DB has no metadata (never appended?)")
        print(
            "CHAIN-VERIFY: status=FAIL breaks=1 first_break_seq=- "
            "first_break_event_id=- first_break_kind=empty-chain lag_min=- unchained=- "
            "forged_below_tip=- genesis_at=- max_seq=-"
        )
        return 1
    recorded_cols = json.loads(recorded_cols_raw)

    live_cols = event_columns(kanban)
    if live_cols != recorded_cols:
        record("-", "-", "schema-drift", f"recorded={recorded_cols} live={live_cols}")

    src_recorded = meta_get(chain, "source_db_path")
    if src_recorded and os.path.abspath(args.db) != src_recorded:
        print(
            f"NOTE: verifying against --db {args.db} which differs from the "
            f"chain's recorded source {src_recorded} (expected during selftest)"
        )

    colsql = ", ".join(f'"{c}"' for c in recorded_cols)
    # G1: proven-legitimate GC prunes are expected-absent, not breaks.
    gc_pruned_ids = {r[0] for r in chain.execute("SELECT event_id FROM gc_pruned")}
    gc_pruned_seen = 0
    prev = genesis_hash()
    last_seq = 0
    last_chained_event_id = None
    last_chained_created_at = None
    chained_ids = set()  # F4: every chained event id, for the forged-insert check
    offset_floor = 0  # walk chain by seq
    while True:
        crows = chain.execute(
            "SELECT seq, event_id, row_hash, prev_hash FROM chain "
            "WHERE seq > ? ORDER BY seq LIMIT ?",
            (offset_floor, BATCH),
        ).fetchall()
        if not crows:
            break
        lo, hi = crows[0][1], crows[-1][1]
        src = {}
        for r in kanban.execute(
            f"SELECT {colsql} FROM {EVENTS_TABLE} WHERE id BETWEEN ? AND ?",
            (lo, hi),
        ):
            src[r["id"]] = r
        for seq, event_id, stored_hash, stored_prev in crows:
            try:
                # (1) chain self-integrity: stored linkage from genesis
                if stored_prev != prev:
                    record(seq, event_id, "chain-link",
                           f"stored_prev={stored_prev[:12]}.. expected={prev[:12]}..")
                row = src.get(event_id)
                if row is None:
                    if event_id in gc_pruned_ids:
                        # G1 (t_78c65b78): proven-legitimate retention prune.
                        # `hermes kanban gc` deletes task_events for terminal
                        # (done/archived) tasks older than the retention
                        # window. Reconciled via `reconcile-gc`, which records
                        # per-event evidence into the sidecar's gc_pruned
                        # ledger. Expected-absent, NOT a break.
                        gc_pruned_seen += 1
                    else:
                        # (2b) deletion: chained event no longer in the DB
                        record(seq, event_id, "deletion", "event_id missing from kanban DB")
                else:
                    # (2a) mutation: recompute from CURRENT DB against stored prev
                    h = row_hash(stored_prev, canonical_json(row, recorded_cols))
                    if h != stored_hash:
                        record(seq, event_id, "mutation",
                               f"recomputed={h[:12]}.. stored={stored_hash[:12]}..")
                    else:
                        if "created_at" in recorded_cols:
                            last_chained_created_at = row["created_at"]
            except Exception as exc:
                # F7: skip-and-flag — a row we cannot verify IS a break
                record(seq, event_id, "verify-error",
                       f"{type(exc).__name__}: {exc}")
            finally:
                prev = stored_hash
                last_seq = seq
                last_chained_event_id = event_id
                chained_ids.add(event_id)
        offset_floor = last_seq

    if last_seq == 0:
        print("ERROR: chain has no rows")
        print(
            "CHAIN-VERIFY: status=FAIL breaks=1 first_break_seq=- "
            "first_break_event_id=- first_break_kind=empty-chain lag_min=- unchained=- "
            "forged_below_tip=- genesis_at=- max_seq=-"
        )
        return 1

    # (4) forged inserts below the tip (F4): every kanban id <= chain tip was
    # chained when first seen, so an id <= tip that is NOT in the chain can
    # only have been inserted after the fact — treated as a break.
    forged_below_tip = 0
    for (fid,) in kanban.execute(
        f"SELECT id FROM {EVENTS_TABLE} WHERE id <= ? ORDER BY id",
        (last_chained_event_id,),
    ):
        if fid not in chained_ids:
            forged_below_tip += 1
            record("-", fid, "forged-insert", "id<=chain tip but never chained")

    # (3) freshness: newest kanban event vs newest chained event.
    # F1: the basis is the created_at OF THE max(id) ROW — never
    # max(created_at), which the live board's year-2033 sentinel rows
    # (ids 20785/21442) would poison into a false CRITICAL lag.
    newest = kanban.execute(
        f"SELECT id, created_at FROM {EVENTS_TABLE} ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if newest is None:
        newest_id, newest_created = None, None
    else:
        newest_id, newest_created = newest["id"], newest["created_at"]
    unchained = kanban.execute(
        f"SELECT count(*) FROM {EVENTS_TABLE} WHERE id > ?",
        (last_chained_event_id,),
    ).fetchone()[0]
    if unchained == 0:
        lag_min = 0.0
    elif newest_created is not None and last_chained_created_at is not None:
        lag_min = max(0.0, (newest_created - last_chained_created_at) / 60.0)
    else:
        lag_min = float("inf")
    lag_breach = args.max_lag_minutes is not None and lag_min > args.max_lag_minutes
    if lag_breach:
        print(
            f"LAG-BREACH: newest kanban event id={newest_id} is {lag_min:.1f} min "
            f"ahead of newest chained event id={last_chained_event_id} "
            f"({unchained} unchained events; limit {args.max_lag_minutes} min)"
        )

    # F5: surface genesis_at + max(seq) so the monitor can alarm on a chain
    # rebuild (new genesis) or truncation (seq drop).
    genesis_at = meta_get(chain, "genesis_at") or "-"

    status = "OK" if (breaks == 0 and not lag_breach) else "FAIL"
    fseq, fev, fkind = (first if first is not None else ("-", "-", "-"))
    print(
        f"CHAIN-VERIFY: status={status} breaks={breaks} first_break_seq={fseq} "
        f"first_break_event_id={fev} first_break_kind={fkind} "
        f"lag_min={lag_min:.1f} unchained={unchained} "
        f"forged_below_tip={forged_below_tip} genesis_at={genesis_at} "
        f"gc_pruned={gc_pruned_seen} "
        f"max_seq={last_seq}"
    )
    return 0 if status == "OK" else 1


# ---------------------------------------------------------- reconcile-gc ----
def cmd_reconcile_gc(args) -> int:
    """G1 (t_78c65b78): classify currently-absent chained events against the
    documented `hermes kanban gc` retention policy and record the ones that
    provably match into the sidecar's gc_pruned ledger.

    POLICY (hermes_cli/kanban_db.py::gc_events):
        DELETE FROM task_events
         WHERE created_at < now - retention
           AND task_id IN (SELECT id FROM tasks WHERE status IN
                           ('done','archived'))

    An absent event is accepted as a legitimate prune ONLY when ALL hold:
      1. the chain recorded its src_task_id/src_created_at (or --backup
         supplies them from a pre-GC snapshot), AND
      2. its owning task still exists in the live DB with status
         done|archived, AND
      3. its src_created_at is older than --retention-days.
    Anything failing a condition is left as a hard break — a deleted event
    whose owner is running/blocked, or that is inside the retention window,
    is NOT explainable by GC and stays loud.

    Refuses to run without --apply (dry-run reports the classification).
    """
    kanban = open_ro(args.db)
    kanban.row_factory = sqlite3.Row
    chain = open_chain(args.chain)
    cutoff = int(time.time()) - int(args.retention_days) * 24 * 3600

    # src metadata: prefer what the chain notarized; fall back to a pre-GC
    # backup snapshot for rows chained before the schema carried src columns.
    src_meta: dict[int, tuple] = {}
    for r in chain.execute(
        "SELECT event_id, src_task_id, src_created_at FROM chain "
        "WHERE src_task_id IS NOT NULL"
    ):
        src_meta[r[0]] = (r[1], r[2])
    backup_used = 0
    if args.backup:
        b = open_ro(args.backup)
        for r in b.execute("SELECT id, task_id, created_at FROM task_events"):
            if r["id"] not in src_meta:
                src_meta[r["id"]] = (r["task_id"], r["created_at"])
                backup_used += 1
        b.close()

    live_events = {r[0] for r in kanban.execute(f"SELECT id FROM {EVENTS_TABLE}")}
    task_status = {r[0]: r[1] for r in kanban.execute("SELECT id, status FROM tasks")}
    already = {r[0] for r in chain.execute("SELECT event_id FROM gc_pruned")}

    accepted, unexplained = [], []
    for (event_id,) in chain.execute("SELECT event_id FROM chain ORDER BY seq"):
        if event_id in live_events or event_id in already:
            continue
        meta = src_meta.get(event_id)
        if meta is None:
            unexplained.append((event_id, "no-src-metadata"))
            continue
        tid, created = meta
        status = task_status.get(tid)
        if status not in ("done", "archived"):
            unexplained.append((event_id, f"owner-status={status}"))
        elif created is None or created >= cutoff:
            unexplained.append((event_id, f"inside-retention created_at={created}"))
        else:
            accepted.append((event_id, tid, created, status))

    evidence = (
        f"hermes-kanban-gc retention_days={args.retention_days} cutoff={cutoff} "
        f"policy=gc_events(created_at<cutoff AND task.status IN (done,archived)) "
        f"reconciled_by=t_78c65b78 backup={args.backup or '-'}"
    )
    if args.apply and accepted:
        now = int(time.time())
        chain.executemany(
            "INSERT OR IGNORE INTO gc_pruned"
            "(event_id,task_id,created_at,task_status,evidence,recorded_at) "
            "VALUES(?,?,?,?,?,?)",
            [(e, t, c, s, evidence, now) for (e, t, c, s) in accepted],
        )
        meta_set(chain, "last_gc_reconcile_at", str(now))
        meta_set(chain, "last_gc_reconcile_evidence", evidence)
        chain.commit()

    for eid, why in unexplained[:20]:
        print(f"UNEXPLAINED event_id={eid} {why}")
    if len(unexplained) > 20:
        print(f"... {len(unexplained) - 20} further unexplained suppressed ...")
    print(
        f"CHAIN-RECONCILE-GC: mode={'apply' if args.apply else 'dry-run'} "
        f"accepted={len(accepted)} unexplained={len(unexplained)} "
        f"already_ledgered={len(already)} backup_sourced_meta={backup_used} "
        f"retention_days={args.retention_days} cutoff={cutoff}"
    )
    return 0 if not unexplained else 1


# --------------------------------------------------------------- selftest ---
def snapshot_db(src: str, dst: str) -> None:
    """Online-backup snapshot (safe on a hot WAL DB — never cp)."""
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


class _Args:
    def __init__(self, db, chain, max_lag_minutes=None):
        self.db = db
        self.chain = chain
        self.max_lag_minutes = max_lag_minutes


def _insert_synthetic_event(db_path: str, new_id: int, created_at: int) -> None:
    """Selftest-only (scratch copies): clone the tip row under a new
    id/created_at so inserts satisfy whatever the live schema requires."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({EVENTS_TABLE})")]
    tip = con.execute(
        f"SELECT * FROM {EVENTS_TABLE} ORDER BY id DESC LIMIT 1"
    ).fetchone()
    vals = []
    for c in cols:
        if c == "id":
            vals.append(new_id)
        elif c == "created_at":
            vals.append(created_at)
        elif c == "payload":
            vals.append('{"selftest":"synthetic"}')
        else:
            vals.append(tip[c])
    colsql = ", ".join(f'"{c}"' for c in cols)
    marks = ", ".join("?" for _ in cols)
    con.execute(f"INSERT INTO {EVENTS_TABLE}({colsql}) VALUES({marks})", vals)
    con.commit()
    con.close()


def _run_verify_capture(vargs):
    """Run cmd_verify (same code path), echo its output, and also return the
    CHAIN-VERIFY summary line for assertions."""
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_verify(vargs)
    out = buf.getvalue()
    sys.stdout.write(out)
    line = ""
    for ln in out.splitlines():
        if ln.startswith("CHAIN-VERIFY:"):
            line = ln
    return rc, line


def cmd_selftest(args) -> int:
    scratch = os.environ.get(
        "KANBAN_CHAIN_SELFTEST_DIR",
        os.path.join(tempfile.gettempdir(), "kanban-chain-selftest"),
    )
    os.makedirs(scratch, exist_ok=True)
    snap = os.path.join(scratch, "snapshot.db")
    chain_db = os.path.join(scratch, "chain.db")
    mut_db = os.path.join(scratch, "tampered-mutation.db")
    del_db = os.path.join(scratch, "tampered-deletion.db")
    lag_db = os.path.join(scratch, "lag.db")
    chain_lag = os.path.join(scratch, "chain-lag.db")
    forged_db = os.path.join(scratch, "forged.db")
    chain_rebuild = os.path.join(scratch, "chain-rebuild.db")
    for p in (snap, chain_db, mut_db, del_db,
              lag_db, chain_lag, forged_db, chain_rebuild):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(p + suffix)
            except FileNotFoundError:
                pass

    print(f"selftest: scratch dir {scratch}")
    print(f"selftest: snapshotting {args.db} -> {snap}")
    snapshot_db(args.db, snap)

    # --- 1. green on known-good -------------------------------------------
    print("\n--- selftest 1/7: build chain on pristine snapshot, expect OK ---")
    rc = cmd_append(_Args(snap, chain_db))
    if rc != 0:
        print("SELFTEST: FAIL (append errored)")
        return 1
    rc_clean, line_clean = _run_verify_capture(_Args(snap, chain_db))
    clean_has_fields = ("genesis_at=" in line_clean and "max_seq=" in line_clean
                        and "forged_below_tip=0" in line_clean)
    print(f"selftest: clean verify exit={rc_clean} new_fields_present={clean_has_fields}")

    # --- 2. red on mutation -------------------------------------------------
    print("\n--- selftest 2/7: mutate one mid-history payload, expect FAIL ---")
    shutil.copyfile(snap, mut_db)  # cold snapshot copy — safe to cp
    con = sqlite3.connect(mut_db)
    mid = con.execute(
        f"SELECT id FROM {EVENTS_TABLE} ORDER BY id "
        f"LIMIT 1 OFFSET (SELECT count(*)/2 FROM {EVENTS_TABLE})"
    ).fetchone()[0]
    con.execute(
        f"UPDATE {EVENTS_TABLE} SET payload = COALESCE(payload,'') || "
        "'{\"tampered\":true}' WHERE id = ?",
        (mid,),
    )
    con.commit()
    con.close()
    print(f"selftest: mutated event id={mid} in {mut_db}")
    rc_mut = cmd_verify(_Args(mut_db, chain_db))
    print(f"selftest: mutation verify exit={rc_mut} (expected 1)")

    # --- 3. red on deletion --------------------------------------------------
    print("\n--- selftest 3/7: delete one chained event row, expect FAIL ---")
    shutil.copyfile(snap, del_db)
    con = sqlite3.connect(del_db)
    victim = con.execute(
        f"SELECT id FROM {EVENTS_TABLE} ORDER BY id "
        f"LIMIT 1 OFFSET (SELECT count(*)/3 FROM {EVENTS_TABLE})"
    ).fetchone()[0]
    con.execute(f"DELETE FROM {EVENTS_TABLE} WHERE id = ?", (victim,))
    con.commit()
    con.close()
    print(f"selftest: deleted event id={victim} from {del_db}")
    rc_del = cmd_verify(_Args(del_db, chain_db))
    print(f"selftest: deletion verify exit={rc_del} (expected 1)")

    # --- 4. lag GREEN with a year-2033 sentinel (F1) --------------------------
    print("\n--- selftest 4/7: lag GREEN with year-2033 sentinel created_at "
          "below the tip (F1 basis = created_at of max(id) row), expect OK ---")
    shutil.copyfile(snap, lag_db)
    con = sqlite3.connect(lag_db)
    base_max = con.execute(f"SELECT max(id) FROM {EVENTS_TABLE}").fetchone()[0]
    con.close()
    now_s = int(time.time())
    _insert_synthetic_event(lag_db, base_max + 1, 2000000000)    # 2033 sentinel
    _insert_synthetic_event(lag_db, base_max + 2, now_s - 7200)  # sane chain tip
    cmd_append(_Args(lag_db, chain_lag))
    _insert_synthetic_event(lag_db, base_max + 3, now_s - 7140)  # unchained, +1 min
    rc_lag_green, line_lg = _run_verify_capture(
        _Args(lag_db, chain_lag, max_lag_minutes=30.0))
    print(f"selftest: lag-green verify exit={rc_lag_green} (expected 0; "
          "max(created_at) IS the 2033 sentinel — the old basis would false-FAIL)")

    # --- 5. lag RED ------------------------------------------------------------
    print("\n--- selftest 5/7: lag RED — unchained event 120 min past the "
          "chained tip, expect FAIL with breaks=0 (lag-only) ---")
    _insert_synthetic_event(lag_db, base_max + 4, now_s)  # 120 min after tip
    rc_lag_red, line_lr = _run_verify_capture(
        _Args(lag_db, chain_lag, max_lag_minutes=30.0))
    lag_red_lag_only = "breaks=0" in line_lr
    print(f"selftest: lag-red verify exit={rc_lag_red} (expected 1) "
          f"lag_only={lag_red_lag_only}")

    # --- 6. forged insert below the tip (F4) ------------------------------------
    print("\n--- selftest 6/7: forged row inserted BELOW the chain tip, "
          "expect FAIL kind=forged-insert ---")
    shutil.copyfile(snap, forged_db)
    con = sqlite3.connect(forged_db)
    min_id = con.execute(f"SELECT min(id) FROM {EVENTS_TABLE}").fetchone()[0]
    con.close()
    _insert_synthetic_event(forged_db, min_id - 1, now_s)
    print(f"selftest: forged event id={min_id - 1} inserted into {forged_db}")
    rc_forged, line_f = _run_verify_capture(_Args(forged_db, chain_db))
    forged_flagged = ("forged_below_tip=1" in line_f
                      and "first_break_kind=forged-insert" in line_f)
    print(f"selftest: forged verify exit={rc_forged} (expected 1) "
          f"forged_flagged={forged_flagged}")

    # --- 7. genesis-change flag (F5) ---------------------------------------------
    print("\n--- selftest 7/7: rebuilt chain must surface a NEW genesis_at ---")
    time.sleep(1.1)  # guarantee a distinct genesis_at second
    cmd_append(_Args(snap, chain_rebuild))
    c1 = open_chain(chain_db)
    g1 = meta_get(c1, "genesis_at")
    c1.close()
    c2 = open_chain(chain_rebuild)
    g2 = meta_get(c2, "genesis_at")
    c2.close()
    rc_gen, line_g = _run_verify_capture(_Args(snap, chain_rebuild))
    genesis_flagged = (g1 is not None and g2 is not None and g1 != g2
                       and f"genesis_at={g2}" in line_g and rc_gen == 0)
    print(f"selftest: genesis-change flag: original genesis_at={g1} "
          f"rebuilt={g2} surfaced_in_verify_line={f'genesis_at={g2}' in line_g} "
          "(the monitor alarms on any change of this value)")

    # --- 8. gc-reconcile: retention-aware deletion classification (G1) -----------
    # A deletion is only excusable when it matches the documented `hermes kanban
    # gc` policy. Two directions, both on the SAME deleted-event fixture:
    #   8a RED  : retention window so wide the event is INSIDE it -> unexplained,
    #             chain still FAILs (a recent deletion is never excused).
    #   8b GREEN: retention window of 0 days (everything is old) AND the owning
    #             task terminal -> accepted, ledgered, verify returns OK.
    print("\n--- selftest 8/8: gc-reconcile classifies the deleted event "
          "(RED inside retention, GREEN when it matches gc policy) ---")

    class _GcArgs(_Args):
        def __init__(self, db, chain, retention_days, apply_, backup=None):
            super().__init__(db, chain)
            self.retention_days = retention_days
            self.apply = apply_
            self.backup = backup

    chain_gc = os.path.join(scratch, "chain-gc.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(chain_gc + suffix)
        except FileNotFoundError:
            pass
    shutil.copyfile(chain_db, chain_gc)

    rc_gc_red = cmd_reconcile_gc(_GcArgs(del_db, chain_gc, 36500, False))
    print(f"selftest: gc dry-run inside-retention exit={rc_gc_red} (expected 1)")

    rc_gc_green = cmd_reconcile_gc(_GcArgs(del_db, chain_gc, 0, True))
    _con = open_chain(chain_gc)
    ledgered = _con.execute(
        "SELECT count(*) FROM gc_pruned WHERE event_id=?", (victim,)
    ).fetchone()[0]
    _con.close()
    rc_gc_verify, line_gc = _run_verify_capture(_Args(del_db, chain_gc))
    gc_green_ok = (rc_gc_green == 0 and ledgered == 1
                   and rc_gc_verify == 0 and "gc_pruned=1" in line_gc)
    print(f"selftest: gc apply-matching-policy exit={rc_gc_green} "
          f"ledgered={ledgered} post-ledger verify exit={rc_gc_verify} "
          f"(expected 0/1/0) — a proven prune stops being a break, an "
          f"unproven one does not")

    ok = (rc_clean == 0 and clean_has_fields
          and rc_mut == 1 and rc_del == 1
          and rc_lag_green == 0
          and rc_lag_red == 1 and lag_red_lag_only
          and rc_forged == 1 and forged_flagged
          and genesis_flagged
          and rc_gc_red == 1 and gc_green_ok)
    print(
        f"\nSELFTEST: {'PASS' if ok else 'FAIL'} "
        f"(clean={rc_clean} fields={clean_has_fields} mutation={rc_mut} "
        f"deletion={rc_del} lag_green={rc_lag_green} lag_red={rc_lag_red} "
        f"lag_red_lag_only={lag_red_lag_only} forged={rc_forged} "
        f"forged_flagged={forged_flagged} genesis_flagged={genesis_flagged} "
        f"gc_red={rc_gc_red} gc_green_ok={gc_green_ok})"
    )
    return 0 if ok else 1


# ------------------------------------------------------------------ main ----
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    for name in ("append", "verify", "selftest", "reconcile-gc"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--chain", default=DEFAULT_CHAIN)
        if name == "verify":
            p.add_argument("--max-lag-minutes", type=float, default=None)
        if name == "reconcile-gc":
            p.add_argument("--retention-days", type=int, default=30,
                           help="must match the `hermes kanban gc` window")
            p.add_argument("--backup", default=None,
                           help="pre-GC snapshot supplying src task_id/created_at "
                                "for events chained before the src columns existed")
            p.add_argument("--apply", action="store_true",
                           help="write the gc_pruned ledger (default dry-run)")
    args = ap.parse_args()
    if args.mode == "append":
        return cmd_append(args)
    if args.mode == "verify":
        return cmd_verify(args)
    if args.mode == "reconcile-gc":
        return cmd_reconcile_gc(args)
    return cmd_selftest(args)


if __name__ == "__main__":
    sys.exit(main())
