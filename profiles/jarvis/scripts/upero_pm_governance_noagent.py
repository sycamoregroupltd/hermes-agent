#!/usr/bin/env python3
"""Deterministic no-agent Upero PM governance shim.

Keeps the Upero governance cron alive when LLM provider route is degraded.
Producer: upero kanban board state.
Consumer: Jarvis cron output/local store + upero dispatcher.

LIVENESS CONTRACT
This runs as a ``no_agent`` cron job, so the process EXIT CODE is the only
signal the mechanism collector consumes: ``jarvis_mechanism_liveness_collect
.classify_job`` marks any ``last_status`` not in ``(None, "ok")`` DEAD, and
stdout is stored for display but never parsed. Two rules follow, and both are
load-bearing:
  1. every real failure must reach the exit code, and
  2. no printed line may claim an action that did not actually happen.
Suppressing a failure to keep the row green would report a broken mechanism as
healthy, which is strictly worse than an honest DEAD row.

DEPENDENCY AUTHORITY
The Hermes kanban engine is the SOLE authority on whether a todo may be
promoted. This script deliberately does NOT re-implement the parent-dependency
gate: it attempts the promote and classifies the engine's own result. A local
predicate would drift from the engine (the real gate satisfies on both ``done``
and ``archived``, and its INNER JOIN ignores dangling parent links) and would
reintroduce a check-then-act race. Only the engine's exact "unsatisfied parent
dependencies" refusal is treated as a safe no-op.
"""
from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path
from typing import NamedTuple, Optional

BOARD = "upero"
DB = Path("/home/frank/.hermes/kanban/boards/upero/kanban.db")
HERMES = "/home/frank/.local/bin/hermes"
NOW = int(time.time())
STALE_SECS = 2 * 60 * 60
KANBAN_TIMEOUT_SECS = 60

# Exact refusal text emitted by the engine's dependency gate
# (hermes_cli/kanban_db.py promote_task -> "unsatisfied parent dependencies: ...").
# The CLI surfaces it as "cannot promote <id>: <reason>" on stderr with rc=1, and
# this script merges stderr into stdout. This is the ONLY tolerated promote
# failure: it means the board is correctly gated, not that the mechanism broke.
UNSATISFIED_PARENT_MARKER = "unsatisfied parent dependencies"


class KanbanResult(NamedTuple):
    """Structured outcome of one ``hermes kanban ...`` invocation."""

    ok: bool
    output: str
    failure: Optional[str]  # None iff ok; otherwise a truthful short reason

    @property
    def is_unsatisfied_parent_refusal(self) -> bool:
        """True only for the engine's dependency-gate refusal.

        Deliberately requires a real nonzero exit carrying the marker. Timeouts,
        missing binaries and spawn errors never populate ``output``, so they can
        never be mistaken for an authorised no-op.
        """
        return (not self.ok) and UNSATISFIED_PARENT_MARKER in self.output


def run_kanban(*args: str) -> KanbanResult:
    """Invoke the kanban CLI and report what actually happened.

    Never raises for an operational failure and never fabricates success. A
    missing binary, a timeout, an OS-level spawn error and any nonzero exit all
    come back as ``ok=False`` with the reason preserved, so the caller can
    decide which are tolerable and surface the rest to the exit code.
    """
    cmd = [HERMES, "kanban", "--board", BOARD, *args]
    label = " ".join(args)
    try:
        cp = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=KANBAN_TIMEOUT_SECS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.output if isinstance(exc.output, str) else ""
        detail = f" :: {partial.strip()}" if partial.strip() else ""
        # Partial output stays out of `output` so a timeout can never be
        # classified as the engine's dependency-gate refusal.
        return KanbanResult(
            False, "", f"timeout after {KANBAN_TIMEOUT_SECS}s: {label}{detail}"
        )
    except FileNotFoundError:
        return KanbanResult(False, "", f"kanban CLI not found at {HERMES}: {label}")
    except OSError as exc:
        return KanbanResult(False, "", f"cannot execute {HERMES}: {exc}: {label}")

    out = (cp.stdout or "").strip()
    if cp.returncode != 0:
        return KanbanResult(False, out, f"rc={cp.returncode}: {label} :: {out}")
    return KanbanResult(True, out, None)


def main() -> int:
    if not DB.exists():
        print(f"UPERO_PM_GOVERNANCE_ERROR: missing board DB {DB}")
        return 1

    actions: list[str] = []  # things that actually happened
    skips: list[str] = []  # engine-authorised no-ops
    degraded: list[str] = []  # real failures -> nonzero exit

    # Read the whole snapshot up front so no cursor is held open across the
    # subprocess calls below. A schema/lock/corruption error here is a genuine
    # store failure and must stay visible rather than degrade into a false OK.
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            counts = {
                r["status"]: r["n"]
                for r in con.execute(
                    "select status, count(*) n from tasks group by status"
                )
            }
            stale_running = [
                r
                for r in con.execute(
                    "select id,title,assignee,"
                    "coalesce(last_heartbeat_at, started_at, created_at) last_seen "
                    "from tasks where status='running'"
                )
                if NOW - int(r["last_seen"] or NOW) > STALE_SECS
            ]
            top_todo = con.execute(
                "select id,title,assignee,priority from tasks where status='todo' "
                "order by priority desc, created_at asc limit 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        print(f"UPERO_PM_GOVERNANCE_ERROR: board DB read failed: {exc}")
        return 1

    # Comment on genuinely stale running work; do not touch fresh running tasks.
    for r in stale_running:
        msg = (
            "deterministic PM nudge: running task has no heartbeat/status update "
            "for over 2h; please post current blocker or complete/block with evidence."
        )
        res = run_kanban("comment", "--author", "upero-pm-governance-noagent", r["id"], msg)
        if res.ok:
            actions.append(f"NUDGED stale running {r['id']} assignee={r['assignee']}")
        else:
            # No comment was written, so no NUDGED claim is made.
            degraded.append(f"COMMENT_FAILED {r['id']}: {res.failure}")

    if counts.get("ready", 0) == 0 and counts.get("todo", 0) > 0 and top_todo is not None:
        # Ask the engine; do not pre-authorise locally. Whatever the engine says
        # is the truth about this promote, evaluated atomically at promote time.
        res = run_kanban("promote", top_todo["id"])
        if res.ok:
            actions.append(
                f"PROMOTED todo {top_todo['id']} priority={top_todo['priority']} "
                f"assignee={top_todo['assignee']}"
            )
        elif res.is_unsatisfied_parent_refusal:
            # Board is correctly gated: a real no-op, not a mechanism failure.
            # Never force-promote, and never claim a promotion happened.
            skips.append(
                f"SKIPPED todo {top_todo['id']} (priority={top_todo['priority']}): engine "
                f"refused promote — unsatisfied parent dependencies; left for the owning "
                f"PM/Frank lane, not force-promoted"
            )
        else:
            degraded.append(f"PROMOTE_FAILED {top_todo['id']}: {res.failure}")

    # Distinct headers: a failure, a real action and an authorised no-op must
    # never be readable as each other.
    if degraded:
        print("UPERO_PM_GOVERNANCE_DEGRADED")
        for d in degraded:
            print(f"- {d}")

    if actions:
        print("UPERO_PM_GOVERNANCE_ACTIONS")
        for a in actions:
            print(f"- {a}")

    if skips:
        print("UPERO_PM_GOVERNANCE_SKIPS")
        for s in skips:
            print(f"- {s}")

    # Nothing printed when there is nothing to report: existing blockers already
    # have PM/Frank-facing cards, so a quiet healthy tick stays quiet.
    return 1 if degraded else 0


if __name__ == "__main__":
    raise SystemExit(main())
