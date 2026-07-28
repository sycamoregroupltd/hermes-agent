#!/usr/bin/env python3
"""Recover / escalate kanban cards stranded on an iteration-budget kill.

The Hermes dispatcher runs goal-mode workers with a bounded iteration budget
(``agent.max_iterations``). When a worker burns all 90 iterations without
calling ``kanban_complete``/``kanban_block``, ``turn_finalizer`` records a
``timed_out`` failure with::

    last_failure_error = "Iteration budget exhausted (90/90) — task could not
                         complete within the allowed iterations"

and the card lands in ``blocked``. Historically the failure classifier
(``kanban_diagnostics.classify_kanban_failure``) returned
``indeterminate`` / ``low`` ("Insufficient decisive evidence...") for any
``last_failure_error`` it did not specifically match — including this one —
so the ``kanban-failure-classifier-cron`` stamped a no-op hint on the card
and it was silently stranded with NO reviewer and NO verdict.

This script is the recovery side of the fix. The diagnostics engine may also
surface the failure for operators, but this actuator deliberately classifies
from the task row itself so it cannot drift against dashboard-only diagnostic
APIs. For every card blocked with that error it:

  * AUTO-RECOVER (bounded) the safe subset:
        - ``consecutive_failures <= 1`` AND no embedded provider/crash error
    by clearing ``last_failure_error`` + resetting the dispatcher failure
    counter (via the sanctioned ``kanban_db.unblock_task`` API, which sets
    blocked cards back to ``ready``/``todo`` and ``consecutive_failures = 0``;
    already-queued ``ready``/``todo`` cards keep their status and only have the
    stale failure marker/counter cleared), so the next dispatch can resume/retry
    with a fresh budget.

  * ESCALATE the repeat/embedded-error subset (``consecutive_failures > 1``
    or an embedded genuine provider/crash error) to a NAMED REVIEWER with a
    recorded verdict — it is NOT auto-retried (it would only re-strand).

Default mode is DRY-RUN: it prints the plan and mutates nothing. Use
``--apply`` to execute the auto-recoveries; escalation always requires
``--apply`` too (it writes a block + reviewer comment).

Exit codes: 0 = nothing to do or dry-run; 0 = applied cleanly;
2 = usage / board error.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES_HOME = os.environ.get("HERMES_HOME", "/home/frank/.hermes/profiles/jarvis")
DEFAULT_BOARD_DIR = Path("/home/frank/.hermes/kanban/boards")
ERROR_PREFIX = "Iteration budget exhausted"

sys.path.insert(0, str(Path(HERMES_HOME) / "scripts"))
# Pull in the hermes CLI package (editable install at hermes-agent).
sys.path.insert(0, "/home/frank/.hermes/hermes-agent")


def connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        sys.exit(f"kanban.db not found: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def classify(conn: sqlite3.Connection) -> dict:
    """Return {auto_recover: [...], escalate: [...], skipped: [...]}.

    The actuator only mutates active queue states (blocked/ready/todo). Rows in
    terminal, scheduled, running, or otherwise out-of-scope states are reported
    as SKIPPED so every dry-run log has an explicit third bucket instead of
    silently hiding stale historical markers.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, assignee, status, block_kind, consecutive_failures,
               last_failure_error
        FROM tasks
        WHERE last_failure_error LIKE ?
        """,
        (ERROR_PREFIX + "%",),
    )
    rows = cur.fetchall()

    auto: list[dict] = []
    escal: list[dict] = []
    skipped: list[dict] = []
    for r in rows:
        tid = r["id"]
        cf = int(r["consecutive_failures"] or 0)
        embedded = _has_embedded_error(conn, tid)
        direct_budget_kill = str(r["last_failure_error"] or "").startswith(ERROR_PREFIX)
        rec = {
            "id": tid,
            "title": (r["title"] or "")[:70],
            "assignee": r["assignee"],
            "status": r["status"],
            "block_kind": r["block_kind"],
            "consecutive_failures": cf,
            "embedded_error": embedded,
            "classification": "budget_exhausted" if direct_budget_kill else "not_budget_exhausted",
            "confidence": "high" if direct_budget_kill else "low",
        }
        if r["status"] not in {"blocked", "ready", "todo"}:
            rec["action"] = "skipped"
            rec["reason"] = f"status_out_of_scope:{r['status']}"
            skipped.append(rec)
        elif direct_budget_kill and not (embedded or cf > 1):
            rec["action"] = "auto_recover"
            rec["reason"] = "cf<=1 and no embedded provider/crash error"
            auto.append(rec)
        else:
            rec["action"] = "escalate"
            rec["reason"] = "cf>1 or embedded provider/crash error"
            escal.append(rec)
    return {"auto_recover": auto, "escalate": escal, "skipped": skipped}


def _has_embedded_error(conn: sqlite3.Connection, task_id: str) -> bool:
    """A genuine provider/crash error buried in the card's history."""
    import re

    PROV = re.compile(
        r"(?i)(RateLimitError|PermissionDeniedError|AuthenticationError|"
        r"HTTP\s+(?:401|402|403|429|5\d\d)|quota|rate[- ]?limit|"
        r"not logged in|invalid api key|unauthorized|forbidden)"
    )
    CRASH = re.compile(
        r"(?i)(pid\s+\d+\s+not alive|exited with code\s+\d+|killed by signal\s+\d+)"
    )
    row = conn.execute(
        "SELECT last_failure_error, result FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    blob = " ".join(str(row[c] or "") for c in ("last_failure_error", "result"))
    if PROV.search(blob) or CRASH.search(blob):
        return True
    for (payload,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ?", (task_id,)
    ):
        if not payload:
            continue
        if PROV.search(str(payload)) or CRASH.search(str(payload)):
            return True
    return False


def reviewer_for(board: str, assignee: str | None) -> str:
    """Pick a sane named reviewer per board/owner convention."""
    mapping = {
        "jarvis-os": "os-reviewer",
        "sycode-trading": "trading-risk-reviewer",
        "upero": "upero-pm",
    }
    return mapping.get(board, "jarvis-os-pm")


def auto_recover(conn: sqlite3.Connection, rec: dict) -> None:
    from hermes_cli import kanban_db as kb

    if rec.get("status") == "blocked":
        # unblock_task clears last_failure_error + consecutive_failures and sets
        # status back to ready/todo (re-gating on parents). This is the sanctioned
        # reset that gives the next dispatch a fresh budget.
        ok = kb.unblock_task(conn, rec["id"])
        if not ok:
            raise RuntimeError(f"unblock_task returned False for {rec['id']}")
    else:
        # A few historical cap kills were already back in the queue (ready/todo)
        # but still carried the stale budget marker. They do not need a status
        # transition; clear only the dispatcher failure fields.
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('ready', 'todo')",
            (rec["id"],),
        )
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (
            rec["id"],
            "budget-exhausted-recovery",
            "[budget-exhausted-recovery t_01b14940] AUTO-RECOVER (bounded): cleared "
            f"Iteration-budget-exhausted error + reset dispatcher failure counter "
            f"(was cf={rec['consecutive_failures']}); re-queued once. If it re-exhausts, "
            "escalate to a named reviewer.",
            now,
        ),
    )


def escalate(conn: sqlite3.Connection, board: str, rec: dict) -> None:
    from hermes_cli import kanban_db as kb

    reviewer = reviewer_for(board, rec["assignee"])
    # Mark blocked with a real block_kind, then route to the named reviewer via
    # the sanctioned assignment API. block_task with kind='needs_input' keeps it
    # in `blocked` awaiting review; already-blocked cards may return False, but
    # assign_task still records the reviewer route and clears the stale retry
    # counter/error for the new assignee/profile combination.
    try:
        kb.block_task(conn, rec["id"], kind="needs_input",
                      reason="iteration budget exhausted (repeat/embedded error) — needs reviewer verdict")
    except Exception:
        pass  # already blocked; status unchanged — verdict comment still applies
    if not kb.assign_task(conn, rec["id"], reviewer):
        raise RuntimeError(f"assign_task returned False for {rec['id']} -> {reviewer}")
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (
            rec["id"],
            "budget-exhausted-recovery",
            f"[budget-exhausted-recovery t_01b14940] ESCALATED to {reviewer}. "
            f"Verdict required: this card hit Iteration-budget-exhausted with "
            f"cf={rec['consecutive_failures']} embedded_error={rec['embedded_error']}. "
            "Do NOT auto-retry (would re-strand). Clear the stale error only after a "
            "verdict, then re-queue once.",
            now,
        ),
    )
    # Verdict is now recorded (comment + named reviewer). Clear the stale
    # budget-error string so the card no longer matches the failed-state grep;
    # it stays blocked awaiting the reviewer (status/block_kind untouched).
    conn.execute(
        "UPDATE tasks SET last_failure_error = NULL WHERE id = ?",
        (rec["id"],),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--board", default="jarvis-os")
    ap.add_argument("--board-dir", type=Path, default=DEFAULT_BOARD_DIR)
    ap.add_argument("--db")
    ap.add_argument("--apply", action="store_true",
                    help="execute auto-recoveries + escalations (default: dry-run)")
    args = ap.parse_args()

    db_path = Path(args.db) if args.db else args.board_dir / args.board / "kanban.db"
    # Autocommit: let ``kb``'s own ``write_tx`` (BEGIN IMMEDIATE /
    # COMMIT) be the only transaction manager. Leaving the default
    # (isolation_level="") open a read transaction on the first SELECT,
    # which then makes ``write_tx``'s nested BEGIN fail.
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        buckets = classify(conn)
    finally:
        conn.close()

    print(f"── budget-exhausted recovery: board={args.board} ("
          f"{'APPLY' if args.apply else 'DRY-RUN'}) ──")
    print(f"  AUTO-RECOVER (bounded retry): {len(buckets['auto_recover'])}")
    for r in buckets["auto_recover"]:
        print(f"    {r['id']}  action={r['action']} class={r['classification']} "
              f"cf={r['consecutive_failures']} reason={r['reason']}  {r['title']}")
    print(f"  ESCALATE to named reviewer : {len(buckets['escalate'])}")
    for r in buckets["escalate"]:
        print(f"    {r['id']}  action={r['action']} class={r['classification']} "
              f"cf={r['consecutive_failures']} embedded={r['embedded_error']} "
              f"reason={r['reason']}  {r['title']}")
    print(f"  SKIPPED (out of scope)     : {len(buckets['skipped'])}")
    for r in buckets["skipped"]:
        print(f"    {r['id']}  action={r['action']} class={r['classification']} "
              f"status={r['status']} cf={r['consecutive_failures']} "
              f"reason={r['reason']}  {r['title']}")

    if not args.apply:
        print("\n  DRY-RUN. Re-run with --apply to execute the recovery/escalation.")
        return 0

    # Autocommit so ``kb`` manages its own transactions.
    conn = connect(db_path)
    conn.isolation_level = None
    try:
        for r in buckets["auto_recover"]:
            auto_recover(conn, r)
        for r in buckets["escalate"]:
            escalate(conn, args.board, r)
    finally:
        conn.close()
    print(f"\n  APPLIED: auto-recovered {len(buckets['auto_recover'])} "
          f"+ escalated {len(buckets['escalate'])} card(s); "
          f"skipped {len(buckets['skipped'])} out-of-scope card(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
