#!/usr/bin/env python3
"""
kanban-reaper.py — de-noise a Hermes kanban board's blocked pile.

The blocked column accumulates crash-casualties: workers that died mid-run
("pid NNNN not alive") or exhausted their iteration budget get parked as
`blocked` at the retry ceiling, burying the handful of genuine gates
(needs_input / dependency). This reaper classifies the pile and acts only
on the provably-safe subset, surfacing the rest for a human/waved decision.

Buckets (NULL-safe on block_kind — see the SQL NULL trap note below):
  KEEP    real gates: block_kind IN ('needs_input','dependency')
  OBSOLETE crashed dupes whose title == a task already status='done'   -> archive (safe, reversible)
  CLONE   crashed, non-obsolete, share a title with another blocked one -> dedup: keep newest, archive rest (only with --dedup)
  GENUINE crashed, unique, real work                                    -> report as requeue candidates (never auto-run)

Default is DRY-RUN (prints the plan, mutates nothing).
  --apply        execute the OBSOLETE archives
  --dedup        additionally archive CLONE duplicates (keep newest per title)
  --requeue N    promote up to N GENUINE survivors back to ready — ONLY after a
                 provider health check passes (doctrine: verify pool, wave it,
                 never bulk-unblock). Requires --apply.

NULL trap: in SQL, `block_kind NOT IN ('a','b')` is NULL (not TRUE) when
block_kind IS NULL, so a naive filter silently drops every NULL-kind row.
Always guard with `(block_kind IS NULL OR block_kind NOT IN (...))`.
"""
import argparse
import os
import subprocess
import sqlite3
import sys

DEFAULT_DB = os.path.expanduser("~/.hermes/kanban/boards/{board}/kanban.db")
REAL_GATES = ("needs_input", "dependency")


def connect_ro(db_path):
    if not os.path.exists(db_path):
        sys.exit(f"kanban.db not found: {db_path}")
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def classify(conn):
    cur = conn.cursor()
    # crashed = blocked with a real failure, excluding genuine human/dep gates (NULL-safe)
    cur.execute(
        """
        SELECT b.id, b.title, b.consecutive_failures, COALESCE(b.priority,''),
               COALESCE(b.last_failure_error,''), COALESCE(b.created_at,'')
        FROM tasks b
        WHERE b.status='blocked' AND b.consecutive_failures>0
          AND (b.block_kind IS NULL OR b.block_kind NOT IN (?,?))
        """,
        REAL_GATES,
    )
    crashed = cur.fetchall()

    done_titles = {r[0] for r in cur.execute("SELECT DISTINCT title FROM tasks WHERE status='done'")}

    obsolete, remaining = [], []
    for row in crashed:
        (obsolete if row[1] in done_titles else remaining).append(row)

    # clones: among remaining, titles that appear more than once
    by_title = {}
    for row in remaining:
        by_title.setdefault(row[1], []).append(row)
    clones, genuine = [], []
    for title, rows in by_title.items():
        if len(rows) > 1:
            rows_sorted = sorted(rows, key=lambda r: r[5])  # created_at asc
            genuine.append(rows_sorted[-1])                 # keep newest as the survivor
            clones.extend(rows_sorted[:-1])                 # archive the older dupes
        else:
            genuine.append(rows[0])

    gates = conn.execute(
        "SELECT id, title, block_kind FROM tasks WHERE status='blocked' AND block_kind IN (?,?)",
        REAL_GATES,
    ).fetchall()
    return dict(obsolete=obsolete, clones=clones, genuine=genuine, gates=gates)


def provider_live():
    """Doctrine: verify the pool before any requeue. A pin is not proof — this
    only confirms the CLI reports a reachable provider, not the served model."""
    try:
        r = subprocess.run(["hermes", "status"], capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and "provider" in (r.stdout + r.stderr).lower()
    except Exception:
        return False


def archive(board, ids):
    if not ids:
        return
    # chunk to keep argv sane
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        subprocess.run(["hermes", "kanban", "--board", board, "archive", *chunk], check=True)


def main():
    ap = argparse.ArgumentParser(description="De-noise a kanban board's blocked crash-casualties.")
    ap.add_argument("--board", default="sycode-trading")
    ap.add_argument("--db")
    ap.add_argument("--apply", action="store_true", help="execute OBSOLETE archives (default: dry-run)")
    ap.add_argument("--dedup", action="store_true", help="also archive CLONE dupes (keep newest per title)")
    ap.add_argument("--requeue", type=int, default=0, metavar="N",
                    help="promote up to N GENUINE survivors to ready (requires --apply + live provider)")
    args = ap.parse_args()

    db_path = args.db or DEFAULT_DB.format(board=args.board)
    conn = connect_ro(db_path)
    c = classify(conn)

    print(f"── kanban-reaper: board={args.board} ──")
    print(f"  KEEP    real gates (needs_input/dependency): {len(c['gates'])}")
    print(f"  OBSOLETE crashed dupes of done tasks        : {len(c['obsolete'])}  -> archive")
    print(f"  CLONE   crashed same-title dupes            : {len(c['clones'])}   -> archive (only with --dedup)")
    print(f"  GENUINE crashed unique real work            : {len(c['genuine'])}  -> requeue candidates")

    if c["gates"]:
        print("\n  Real gates still needing a human:")
        for tid, title, kind in c["gates"]:
            print(f"    [{kind}] {tid}  {title[:60]}")

    to_archive = [r[0] for r in c["obsolete"]]
    if args.dedup:
        to_archive += [r[0] for r in c["clones"]]

    if not args.apply:
        print(f"\n  DRY-RUN. Would archive {len(to_archive)} task(s). Re-run with --apply to execute.")
        if args.requeue:
            print(f"  Would then attempt to requeue up to {args.requeue} genuine survivor(s) (provider-gated).")
        return

    print(f"\n  APPLY: archiving {len(to_archive)} task(s)...")
    archive(args.board, to_archive)
    print("  archived.")

    if args.requeue > 0:
        if not provider_live():
            print("  REQUEUE SKIPPED: provider health check failed — not stampeding a dead pool.")
            return
        wave = [r[0] for r in sorted(c["genuine"], key=lambda r: r[2])][: args.requeue]  # lowest-failure first
        print(f"  REQUEUE: provider live; promoting {len(wave)} survivor(s) to ready (of {len(c['genuine'])} genuine)...")
        subprocess.run(["hermes", "kanban", "--board", args.board, "unblock", *wave], check=True)
        print(f"  requeued {len(wave)}. Remaining genuine survivors NOT touched: {len(c['genuine']) - len(wave)}")


if __name__ == "__main__":
    main()
