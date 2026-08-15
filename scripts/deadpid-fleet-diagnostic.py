#!/usr/bin/env python3
"""deadpid-fleet-diagnostic.py — reusable fleet-wide dead-PID detection + safe routing.

Part of the Jarvis-OS CEO-decompose fix for the "systemic dead-PID failure class"
(t_9e894c1d). This is the READ-ONLY diagnostic core. It does NOT mutate any board.

WHAT IT DOES
  1. Scans every board DB under HERMES_KANBAN_HOME for tasks whose
     `last_failure_error` matches the dead-PID signature (`pid <N> not alive`)
     and whose status is terminal (blocked / gave_up / archived / scheduled)
     or whose block_kind is a recovery gate. These are "dead-PID residuals" —
     tasks that already crashed on a dead worker PID but whose human-visible
     label (from `kanban classify-failure`) is often WRONG.
  2. For each residual, it reports the DISCREPANCY between:
       - what the dispatcher's failure_classifier AUTO-told humans (often
         provider_pre_reasoning / skill_preload_crash / indeterminate), and
       - the GROUND TRUTH in `last_failure_error` (a dead worker PID).
  3. Optionally re-runs the authoritative reaper-only liveness check on tasks
     still `running` with a `worker_pid`, to find the live observability gap.

WHY THIS EXISTS (root cause)
  `detect_crashed_workers` correctly reaps dead-PID workers back to `ready`
  (and trips the breaker to `blocked`/`gave_up`). That part WORKS.
  But the *failure classifier* (`hermes kanban classify-failure` ->
  `classify_kanban_failure`) reads task TITLE/BODY/SKILLS text and,
  because many tasks literally ask to fix "pid_not_alive" or list a skill that
  failed to preload, it mis-classifies the dead-PID crash as
  `skill_preload_crash` / `provider_pre_reasoning`. Operators then have to
  re-open every board and re-diagnose by hand -> the "manual per-board clear"
  the CEO contract asked us to eliminate.

SAFE-ROUTING SEMANTIC
  This script never writes. To REMEDIATE (only where evidence is unambiguous
  and a human has approved), pair it with `hermes kanban reclaim <id>` /
  `hermes kanban --board <b> unblock <id>` or a code fix that makes the
  classifier precedence `dead-PID error string > body-text heuristics`.

USAGE
  python3 deadpid-fleet-diagnostic.py            # full scan, human summary
  python3 deadpid-fleet-diagnostic.py --json     # machine output
  python3 deadpid-fleet-diagnostic.py --only-running-dead   # live gap only
"""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sqlite3

KANBAN_HOME = os.environ.get("HERMES_KANBAN_HOME", "/home/frank/.hermes/kanban")
DEADPID_RE = re.compile(r"pid\s+\d+\s+not alive", re.I)

# Failure classes the dispatcher's classifier is allowed to report for a
# genuine dead-PID crash. Anything else on a task whose `last_failure_error`
# is a dead-PID string is a MISLABEL that forced manual diagnosis.
DEADPID_CORRECT_CLASSES = {
    "pid_not_alive_or_nonzero_crash",
    "skill_preload_crash",   # sometimes legit (worker died loading a skill)
}
TERMINAL_STATUSES = {"blocked", "gave_up", "archived", "scheduled"}
RECOVERY_BLOCK_KINDS = {"capability", "needs_input", "transient", "dependency"}


def _connect_ro(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _board_db_paths() -> list[str]:
    out = sorted(glob.glob(os.path.join(KANBAN_HOME, "boards", "*", "kanban.db")))
    for extra in glob.glob(os.path.join(KANBAN_HOME, "*.db")):
        if os.path.basename(extra) not in ("db", "db.db", "store.duckdb"):
            out.append(extra)
    return out


def _last_failure_class(con, cur, tabs, task_id) -> str | None:
    if "task_events" not in tabs:
        return None
    row = cur.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='failure_classified' "
        "ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0]).get("failure_class")
    except Exception:
        return None


def scan_residuals() -> list[dict]:
    rows_out: list[dict] = []
    for db in _board_db_paths():
        slug = os.path.basename(os.path.dirname(db))
        try:
            con = _connect_ro(db); con.row_factory = sqlite3.Row; cur = con.cursor()
            tabs = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            if "tasks" not in tabs:
                con.close(); continue
            for r in cur.execute(
                "SELECT id,status,assignee,block_kind,last_failure_error,"
                "worker_pid,consecutive_failures FROM tasks"
            ).fetchall():
                err = r["last_failure_error"] or ""
                if not DEADPID_RE.search(err):
                    continue
                status = r["status"] or ""
                bk = r["block_kind"] or ""
                if not (status in TERMINAL_STATUSES or bk in RECOVERY_BLOCK_KINDS):
                    continue
                fc = _last_failure_class(con, cur, tabs, r["id"])
                rows_out.append(dict(
                    board=slug, id=r["id"], status=status, block_kind=bk,
                    assignee=r["assignee"], consecutive_failures=r["consecutive_failures"],
                    failure_classified=fc, last_failure_error=err[:200],
                    mislabeled=(fc not in DEADPID_CORRECT_CLASSES),
                ))
            con.close()
        except Exception as ex:  # pragma: no cover
            print(f"  ! {db}: {ex}")
    return rows_out


def scan_running_dead() -> list[dict]:
    out: list[dict] = []
    for db in _board_db_paths():
        slug = os.path.basename(os.path.dirname(db))
        try:
            con = _connect_ro(db); con.row_factory = sqlite3.Row; cur = con.cursor()
            for r in cur.execute(
                "SELECT id,status,worker_pid,claim_lock,claim_expires FROM tasks "
                "WHERE status='running' AND worker_pid IS NOT NULL"
            ).fetchall():
                pid = int(r["worker_pid"])
                alive = True
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError, OSError):
                    alive = False
                if not alive:
                    out.append(dict(
                        board=slug, id=r["id"], worker_pid=pid,
                        claim_lock=r["claim_lock"],
                        claim_expires=int(r["claim_expires"]) if r["claim_expires"] else None,
                    ))
            con.close()
        except Exception as ex:  # pragma: no cover
            print(f"  ! {db}: {ex}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Fleet dead-PID detection (read-only)")
    ap.add_argument("--json", action="store_true", help="emit machine JSON")
    ap.add_argument("--only-running-dead", action="store_true",
                    help="only report running tasks with a dead worker PID")
    args = ap.parse_args()

    if args.only_running_dead:
        rd = scan_running_dead()
        if args.json:
            print(json.dumps(rd, indent=2))
        else:
            print(f"RUNNING tasks with a DEAD worker_pid: {len(rd)}")
            for x in rd:
                print(f"  {x['board']}/{x['id']}  worker_pid={x['worker_pid']}  "
                      f"claim_expires={x['claim_expires']}")
        return 0

    residuals = scan_residuals()
    mislabeled = [r for r in residuals if r["mislabeled"]]
    by_board = collections.Counter(r["board"] for r in residuals)
    by_fclass = collections.Counter(r["failure_classified"] or "none/uncclassified" for r in residuals)

    if args.json:
        print(json.dumps(dict(
            total=len(residuals), mislabeled=len(mislabeled),
            by_board=dict(by_board), by_failure_class=dict(by_fclass),
            samples=residuals[:25],
        ), indent=2))
    else:
        print("=" * 72)
        print("FLEET DEAD-PID RESIDUAL SCAN (read-only)")
        print("=" * 72)
        print(f"Total dead-PID residuals:        {len(residuals)}")
        print(f"  Mislabeled by classifier:     {len(mislabeled)} "
              f"({100*len(mislabeled)/max(len(residuals),1):.0f}%)  <- forced manual unblock")
        print("\nBy board:")
        for b, c in by_board.most_common():
            print(f"  {b}: {c}")
        print("\nBy auto failure_classified (what humans were TOLD):")
        for f, c in by_fclass.most_common():
            print(f"  {f}: {c}")
        print("\nInterpretation:")
        print("  - The reaper (detect_crashed_workers) already AUTO-resolved these.")
        print("  - The dead-PID ERROR STRING is ground truth; the failure_classified")
        print("    label is often wrong and is what forced per-board human triage.")
        print("  - Remediation = code fix giving 'pid <N> not alive' precedence over")
        print("    body-text heuristics, + a fleet diagnostic cron (this script).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
