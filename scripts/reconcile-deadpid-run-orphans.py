#!/usr/bin/env python3
"""reconcile-deadpid-run-orphans.py — close stray open `task_runs` rows on
TERMINAL kanban tasks (the `running_run_dead_pid` integrity-invariant violation).

WHY THIS EXISTS (t_94b12918 / t_2c58ce89)
  The fleet kanban integrity canary (jarvis_os_kanban_integrity_backup.py, cron
  93ced04b18bf) enforces "terminal task => no open `running` run". It DETECTS
  but never RECONCILES: a `task_runs` row left `status='running'` with a dead
  `worker_pid` after its parent task already reached `done`/`archived` violates
  the invariant and keeps the canary red forever. The native dispatcher reapers
  (reclaim_task / detect_crashed_workers / release_stale_claims) only select
  `tasks WHERE status='running'`, so a terminal task with a stray open run is
  INVISIBLE to them. This script closes exactly that gap and can run as a
  durable guard inside the self-heal OR standalone.

SAFETY INVARIANTS (t_94b12918 gates):
  * TERMINAL-ONLY close: a run is closed only if its parent task status is in
    TERMINAL_STATUSES. Non-terminal tasks are left to the dispatcher reaper.
  * DEAD-PID CONFIRMED before any close (kill -0 + /proc zombie check).
  * LIVE WORKERS NEVER TOUCHED: non-terminal runs whose PID is still alive are
    skipped entirely (no false abort, no reaping a healthy worker like the
    current executor). Non-terminal runs whose PID is dead are REPORTED as an
    escalation but NOT auto-closed (that is the dispatcher's job; we must not
    reopen/duplicate-spawn). This fixes a defect in the original os-reviewer
    draft, whose guard aborted on ANY non-terminal open run.
  * BOUND PARAMETERS ONLY: no string-formatted SQL for values.
  * IDEMPOTENT: re-running closes nothing the second time.
  * task_runs ONLY: tasks.status / claim_lock / worker_pid are never written.
  * REVERSIBLE: run-row mutation only; the canary backup is the restore point.

Usage:
  python3 reconcile-deadpid-run-orphans.py --dry-run [--boards jarvis-os,sycode-trading]
  python3 reconcile-deadpid-run-orphans.py            # mutate
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from hermes_cli import kanban_db as kb

KANBAN_HOME = os.environ.get("HERMES_KANBAN_HOME", "/home/frank/.hermes/kanban")
TERMINAL_STATUSES = ("done", "archived", "cancelled")
RECONCILED = "reconciled"


# --------------------------------------------------------------------------
# pid liveness — mirrors hermes hermes_cli/kanban_db.py:_pid_alive (POSIX)
# --------------------------------------------------------------------------
def _pid_alive(pid) -> bool:
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)  # ProcessLookupError / PermissionError surfaced below
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    # Still here -> process exists. On Linux, treat zombie (State: Z) as dead.
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid_int}/status", "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("State:"):
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            return False  # proc entry gone -> already reaped
    return True


def _board_db_paths(slugs: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for slug in slugs:
        db = (
            os.path.join(KANBAN_HOME, "kanban.db")
            if slug == "default"
            else os.path.join(KANBAN_HOME, "boards", slug, "kanban.db")
        )
        if os.path.exists(db):
            out.append((slug, db))
    return out


def _find_runs(conn) -> tuple[list, list, list]:
    """Return (terminal_orphans_to_close, nonterminal_dead_escalations, live_skipped)."""
    q = (
        "SELECT r.id, r.task_id, r.worker_pid, r.last_heartbeat_at, t.status "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.status = ? AND r.worker_pid IS NOT NULL AND r.ended_at IS NULL"
    )
    terminal_orphans: list[dict] = []
    nonterminal_dead: list[dict] = []
    live_skipped: list[dict] = []
    for row in conn.execute(q, ("running",)):
        task_status = row["status"]
        pid = row["worker_pid"]
        alive = _pid_alive(pid)
        rec = {
            "run_id": row["id"],
            "task_id": row["task_id"],
            "worker_pid": pid,
            "last_heartbeat_at": row["last_heartbeat_at"],
            "task_status": task_status,
        }
        if alive:
            live_skipped.append(rec)  # never touch a live worker
            continue
        if task_status in TERMINAL_STATUSES:
            terminal_orphans.append(rec)  # safe to close
        else:
            nonterminal_dead.append(rec)  # dispatcher's job; escalate, don't close
    return terminal_orphans, nonterminal_dead, live_skipped


def _close_orphan(conn, run_id: int, now: int) -> None:
    conn.execute(
        "UPDATE task_runs SET status = ?, outcome = ?, ended_at = ?, worker_pid = NULL "
        "WHERE id = ? AND status = 'running' AND ended_at IS NULL",
        (RECONCILED, RECONCILED, now, run_id),
    )


def reconcile_board(slug: str, db_path: str, dry_run: bool) -> dict:
    out = {"board": slug, "db": db_path, "to_close": 0, "closed": 0,
           "escalations": 0, "live_skipped": 0}
    try:
        ro = kb.connect(db_path=Path(db_path))
        try:
            terminal_orphans, nonterminal_dead, live_skipped = _find_runs(ro)
        finally:
            ro.close()
    except Exception as ex:
        out["error"] = f"read phase: {type(ex).__name__}: {ex}"
        return out

    out["to_close"] = len(terminal_orphans)
    out["escalations"] = len(nonterminal_dead)
    out["live_skipped"] = len(live_skipped)
    if nonterminal_dead:
        out["escalation_detail"] = [
            {"run_id": d["run_id"], "task_id": d["task_id"], "task_status": d["task_status"]}
            for d in nonterminal_dead
        ]

    if dry_run:
        out["mode"] = "dry-run"
        return out
    if not terminal_orphans:
        out["mode"] = "mutate"
        return out

    try:
        w = kb.connect(db_path=Path(db_path))
        w.isolation_level = None
        try:
            w.execute("BEGIN IMMEDIATE")
            now = int(time.time())
            for o in terminal_orphans:
                _close_orphan(w, o["run_id"], now)
            w.execute("COMMIT")
            out["closed"] = len(terminal_orphans)
            out["mode"] = "mutate"
        except Exception as ex:
            try:
                w.execute("ROLLBACK")
            except Exception:
                pass
            out["error"] = f"write phase: {type(ex).__name__}: {ex}"
        finally:
            w.close()
    except Exception as ex:
        out["error"] = f"connect(write): {type(ex).__name__}: {ex}"
    return out


def _board_slugs() -> list[str]:
    raw = os.environ.get("HERMES_KANBAN_INTEGRITY_BOARDS", "")
    if raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [
        os.path.basename(os.path.dirname(p))
        for p in __import__("glob").glob(os.path.join(KANBAN_HOME, "boards", "*", "kanban.db"))
    ]


def main() -> int:
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    slugs = _board_slugs()
    for i, a in enumerate(argv):
        if a == "--boards" and i + 1 < len(argv):
            slugs = [s.strip() for s in argv[i + 1].split(",") if s.strip()]

    results = []
    for slug, db_path in _board_db_paths(slugs):
        res = reconcile_board(slug, db_path, dry_run)
        results.append(res)

    for r in results:
        bits = (
            f"[{r['board']}] to_close={r.get('to_close', 0)} "
            f"closed={r.get('closed', 0)} escalations={r.get('escalations', 0)} "
            f"live_skipped={r.get('live_skipped', 0)}"
        )
        if r.get("error"):
            bits += f" ERROR={r['error']}"
        (sys.stderr if r.get("error") else sys.stdout).write(bits + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
