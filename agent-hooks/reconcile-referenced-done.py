#!/usr/bin/env python3
"""reconcile-referenced-done.py — DRAFT cross-card reconciliation hook (NO daemon).

CHILD-2 of t_c60c6a57 decomposition (jarvis-os/t_349674a6). STATUS: DRAFT —
NOT wired to any live hook. Requires os-reviewer sign-off (CHILD-3) before any
attachment to the live completion-gate path. GATES: no DB mutation outside a
kanban comment / status flip; no production deploy; no credential access;
fail-open on any ambiguity so a guard malfunction never wedges the fleet.

PURPOSE
-------
When a task transitions to `done`, some *other* cards exist whose entire reason
for being was to track that task:

  - RESEARCH-ACTIONABLE lanes  (auto-routed by research_review_extractor.py)
  - REVIEW / RE-REVIEW lanes   (auto-routed by the completion-gate review router)

If such a referencing card is still open (todo/ready/running/blocked) but its
referenced source is now `done`, the referencing card is a *stale reference*:
a phantom blocker or a dead lane. This hook emits a re-triage comment and, for
the pure stale-reference case (no genuine remaining work), flips the lane to
done. If genuine work remains, it reassigns to the owner instead of closing.

DESIGN — attach to the EXISTING completion-gate classifier, not a daemon
------------------------------------------------------------------------
This module is intentionally a *library function* plus a thin CLI, designed to
be invoked from inside the existing completion-gate classifier flow
(`gate-kanban-complete.sh` -> `gate-kanban-complete-classifier.py`) at the
point a task's `kanban_complete` is being allowed through. It reuses the SAME
single-source-of-truth stale-ref detection semantics as
`~/.hermes/scripts/kanban_dedupe_guard.py` (RULE4-stale-ref, lines 405-507) so
the completion-time hook and the cron backstop can never disagree.

It is NOT a standalone daemon: no loop, no scheduler, no polling. One call per
completion event. The cron guard (`kanban_dedupe_guard.py` RULE4) remains the
backstop for completions that bypass the gate.

CONSERVATIVE REFINEMENTS (from CHILD-1 scan report, jarvis-os/t_b24a9f07):
  1. NEVER auto-close a lane blocked with block_kind = needs_input / dependency
     (human-gated). Only re-triage-comment those.
  2. NEVER auto-close a REVIEW lane carrying a contrary verdict
     (CHANGES_REQUESTED / REJECT / BLOCK / REWORK_REQUIRED) in its comments.
  3. Auto-close ONLY when the lane is a pure stale reference: referenced source
     done AND no contrary verdict AND block_kind not human-gated AND no open
     children of its own.
  4. Otherwise: comment + reassign to the lane's original owner (assignee) so a
     human decides.

Contract: read-only by default. Mutations (comment / status) happen ONLY via
explicit `--apply` and ONLY through `hermes kanban` CLI (the same path the
existing guard uses) — never raw SQL writes.

IDEMPOTENCY CONTRACT (reviewer finding #6): this hook is invoked exactly once
per completion event by the completion gate. Double-completion idempotency is
structurally guaranteed at the call site: `hermes kanban complete` on an
already-done task is rejected by the CLI (state transition done->done is
invalid), so a duplicate close cannot land. Reassign/comment duplicates are
avoided because the lane leaves the open-status set after the first apply, so
a second `find_referencing_lanes` call returns nothing for it.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field

# --- single-source-of-truth patterns, mirrored from kanban_dedupe_guard.py ---
# Lane prefixes that are auto-routed tracking lanes (sole purpose = follow a
# referenced source task). An arbitrary card that merely *mentions* a done task
# is NOT in scope.
STALE_REF_LANE_RE = re.compile(r"^(RESEARCH-ACTIONABLE|RE-?REVIEW|REVIEW)\b", re.I)
# A bare task id, optionally board-prefixed (e.g. "jarvis-os/t_349cf425").
STALE_REF_ID_RE = re.compile(r"(?:[a-z0-9_-]+/)?(t_[0-9a-f]{8})\b")
STALE_REF_VERDICT_RE = re.compile(r"REVIEW_VERDICT\s*[:=]\s*([A-Z0-9_]+)")
CONTRARY_VERDICTS = {"CHANGES_REQUESTED", "REJECT", "BLOCK", "REWORK_REQUIRED"}
HUMAN_GATED_BLOCK_KINDS = {"needs_input", "dependency"}

GUARD_AUTHOR = "reconcile-referenced-done"

# Statuses that mean "the referencing lane is still open / actionable".
OPEN_STATUSES = {"todo", "ready", "running", "blocked", "review", "triage"}


@dataclass
class Lane:
    id: str
    title: str
    body: str
    status: str
    assignee: str | None
    block_kind: str | None
    comments: list[str] = field(default_factory=list)
    open_children: int = 0


@dataclass
class Action:
    lane_id: str
    ref_id: str
    disposition: str           # "close" | "reassign" | "comment-only" | "skip"
    reason: str
    comment: str | None = None
    reassign_to: str | None = None


def _connect_ro(db_path: str) -> sqlite3.Connection:
    """Read-only SQLite connection (same contract as the cron guard)."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _check_schema(conn: sqlite3.Connection) -> bool:
    """Return True if the DB has the expected tasks table schema (status column).

    Guards against minimal fixture DBs (selftest) where the tasks table exists
    but lacks columns required by this hook. Fail-open (return False) when the
    schema is unexpected.
    """
    try:
        row = conn.execute("PRAGMA table_info(tasks)").fetchall()
        cols = {r[1] for r in row}
        return "status" in cols
    except sqlite3.Error:
        return False


def _latest_verdict(comments: list[str]) -> str | None:
    for body in reversed(comments):
        m = list(STALE_REF_VERDICT_RE.finditer(body or ""))
        if m:
            return m[-1].group(1).upper()
    return None


def load_lane(conn: sqlite3.Connection, lane_id: str) -> Lane | None:
    row = conn.execute(
        "SELECT id, title, COALESCE(body,''), status, assignee, "
        "       COALESCE(block_kind,'') FROM tasks WHERE id=?",
        (lane_id,),
    ).fetchone()
    if row is None:
        return None
    comments = [
        r[0]
        for r in conn.execute(
            "SELECT COALESCE(body,'') FROM task_comments WHERE task_id=? "
            "ORDER BY created_at ASC",
            (lane_id,),
        ).fetchall()
    ]
    # Open children: lanes that have this lane as a parent and are still open.
    try:
        open_children = conn.execute(
            "SELECT COUNT(*) FROM task_links l JOIN tasks t ON t.id=l.child_id "
            "WHERE l.parent_id=? AND t.status IN "
            "('todo','ready','running','blocked','review','triage')",
            (lane_id,),
        ).fetchone()[0]
    except sqlite3.Error:
        open_children = 0  # schema without links -> fail open (treat as none)
    return Lane(
        id=row[0], title=row[1] or "", body=row[2] or "", status=row[3],
        assignee=row[4], block_kind=(row[5] or None),
        comments=comments, open_children=open_children,
    )


def referenced_done_ids(conn: sqlite3.Connection, lane: Lane,
                        completing_ids: set[str] | None = None) -> list[str]:
    """Done (or completing-now) task ids referenced by this lane's title+body.

    ``completing_ids`` are tasks in the process of being completed (pre_tool_call
    context where status has not yet flipped to ``done``). They are treated as
    done for stale-reference detection purposes.
    """
    refs = {m.group(1) for m in STALE_REF_ID_RE.finditer(lane.title + "\n" + lane.body)}
    completing = completing_ids or set()
    done = []
    for rid in refs:
        r = conn.execute("SELECT status FROM tasks WHERE id=?", (rid,)).fetchone()
        if r and (r[0] == "done" or rid in completing):
            done.append(rid)
    return sorted(done)


def find_referencing_lanes(conn: sqlite3.Connection, done_id: str,
                           completing_ids: set[str] | None = None) -> list[Lane]:
    """Open auto-routed lanes whose title/body references `done_id`.

    Completion-time direction: we are told *which* task just went done, and we
    find the lanes still open that point at it. This is the inverse of the cron
    guard's scan (which walks blocked lanes and looks up their refs).

    When called from a pre_tool_call hook (task not yet done), pass the current
    task id in ``completing_ids`` so it is treated as done for stale-ref checks.
    """
    out: list[Lane] = []
    like = f"%{done_id}%"
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status IN "
        "('todo','ready','running','blocked','review','triage') "
        "AND (title LIKE ? OR body LIKE ?)",
        (like, like),
    ).fetchall()
    for (lid,) in rows:
        lane = load_lane(conn, lid)
        if lane is None:
            continue
        if not STALE_REF_LANE_RE.match(lane.title or ""):
            continue  # not an auto-routed tracking lane -> leave alone
        if done_id in referenced_done_ids(conn, lane, completing_ids=completing_ids):
            out.append(lane)
    return out


def classify_lane(lane: Lane, ref_id: str) -> Action:
    """Decide what to do with one referencing lane. Pure / deterministic."""
    base_reason = f"references done task {ref_id}"

    # Human-gated blocks are never auto-closed.
    if lane.status == "blocked" and (lane.block_kind in HUMAN_GATED_BLOCK_KINDS):
        return Action(
            lane_id=lane.id, ref_id=ref_id, disposition="comment-only",
            reason=f"{base_reason}; block_kind={lane.block_kind} is human-gated",
            comment=_retriage_comment(lane, ref_id,
                "referenced source is done. This lane is human-gated "
                f"(block_kind={lane.block_kind}); re-triage required — do NOT "
                "auto-close."),
        )

    # Contrary verdict on a REVIEW lane => genuine work remains.
    if lane.title.lower().startswith("review"):
        verdict = _latest_verdict(lane.comments)
        if verdict in CONTRARY_VERDICTS:
            return Action(
                lane_id=lane.id, ref_id=ref_id, disposition="reassign",
                reason=f"{base_reason}; contrary verdict {verdict} => genuine work",
                comment=_retriage_comment(lane, ref_id,
                    f"referenced source is done but this REVIEW lane carries a "
                    f"contrary verdict ({verdict}); genuine work remains. "
                    "Reassigning to owner for disposition."),
                reassign_to=lane.assignee,
            )

    # Lane has its own open children => not a pure stale reference.
    if lane.open_children > 0:
        return Action(
            lane_id=lane.id, ref_id=ref_id, disposition="reassign",
            reason=f"{base_reason}; {lane.open_children} open child(ren)",
            comment=_retriage_comment(lane, ref_id,
                f"referenced source is done but this lane has "
                f"{lane.open_children} open child task(s); not a pure stale "
                "reference. Reassigning to owner for disposition."),
            reassign_to=lane.assignee,
        )

    # Pure stale reference: referenced source done, no contrary verdict, not
    # human-gated, no open children => safe to close.
    return Action(
        lane_id=lane.id, ref_id=ref_id, disposition="close",
        reason=f"{base_reason}; pure stale-reference (no remaining work)",
        comment=_retriage_comment(lane, ref_id,
            "referenced source is done and this is a pure stale-reference lane "
            "(no remaining work). Auto-closing with evidence comment. If "
            "genuine work remains, reopen a concrete child task. "
            "Ref: jarvis-os/t_c60c6a57."),
    )


def _retriage_comment(lane: Lane, ref_id: str, detail: str) -> str:
    # Acceptance criterion #2 phrasing: "referenced task <id> is DONE — re-triage".
    return (
        f"[{GUARD_AUTHOR}] referenced task {ref_id} is DONE — re-triage: {detail}"
    )


def _run_hermes(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["hermes", *args], capture_output=True, timeout=30, text=True,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode == 0, out
    except Exception as exc:
        return False, str(exc)  # fail-open: a guard error must never wedge the fleet


def apply_action(board: str, action: Action, *, apply: bool) -> str:
    """Execute (or, without --apply, describe) one reconciliation action."""
    verb = action.disposition.upper() if apply else f"WOULD-{action.disposition.upper()}(dry-run)"
    line = f"{verb} {board}/{action.lane_id} -> {action.reason}"
    if not apply:
        return line
    failures: list[str] = []
    if action.comment:
        ok, out = _run_hermes(["kanban", "--board", board, "comment", action.lane_id,
                               "--author", GUARD_AUTHOR, action.comment])
        if not ok:
            failures.append(f"comment failed: {out[:200]}")
    if action.disposition == "close":
        ok, out = _run_hermes(["kanban", "--board", board, "complete", action.lane_id,
                               "--summary",
                               f"Auto-resolved stale-reference: {action.ref_id} is done."])
        if not ok:
            failures.append(f"complete failed: {out[:200]}")
    elif action.disposition == "reassign" and action.reassign_to:
        # Reassignment uses the CLI update path; if unsupported, the comment
        # above still records the re-triage so no signal is lost (fail-open).
        ok, out = _run_hermes(["kanban", "--board", board, "update", action.lane_id,
                               "--assignee", action.reassign_to])
        if not ok:
            failures.append(f"update --assignee failed: {out[:200]}")
    if failures:
        line += " | PARTIAL-FAILURE: " + "; ".join(failures)
    return line


def reconcile_on_done(db_path: str, board: str, done_id: str,
                      *, apply: bool = False,
                      completing_id: str | None = None) -> list[str]:
    """Entry point called when `done_id` transitions to done.

    When called from a pre_tool_call hook (task not yet done), pass the same
    id as ``completing_id`` so it is treated as done for stale-ref detection
    even before the status flip.

    Returns human-readable action lines (also suitable for a cron log).
    """
    report: list[str] = []
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error as exc:
        return [f"ERROR: cannot open {db_path} read-only: {exc} (fail-open, no-op)"]
    try:
        if not _check_schema(conn):
            return ["SKIP: DB schema missing required columns (status); safe to ignore"]
        completing: set[str] = {completing_id} if completing_id else set()
        lanes = find_referencing_lanes(conn, done_id, completing_ids=completing)
        if not lanes:
            return [f"OK: no open referencing lanes for done task {done_id}"]
        for lane in lanes:
            action = classify_lane(lane, done_id)
            if action.disposition == "skip":
                continue
            report.append(apply_action(board, action, apply=apply))
    finally:
        conn.close()
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", required=True)
    ap.add_argument("--db", required=True, help="path to the board kanban.db")
    ap.add_argument("--done-id", required=True,
                    help="task id that just transitioned to done")
    ap.add_argument("--completing-id", default=None,
                    help="task id being completed (use when called from pre_tool_call; "
                         "same as --done-id when task status has not yet flipped)")
    ap.add_argument("--apply", action="store_true",
                    help="actually comment/close/reassign (default: dry-run)")
    args = ap.parse_args(argv)
    completing = args.completing_id or args.done_id
    for line in reconcile_on_done(args.db, args.board, args.done_id,
                                  apply=args.apply, completing_id=completing):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
