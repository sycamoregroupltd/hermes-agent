#!/usr/bin/env python3
"""test_reconcile_referenced_done.py — unit-style test for the CHILD-2 draft hook.

Drives reconcile-referenced-done.py against an IN-MEMORY SQLite fixture (no
real board DB touched, no `hermes` CLI invoked — dry-run mode only). Models the
seed case t_7cca7076 -> t_349cf425 (referenced done 2026-07-05 21:30) and the
two live pairs found by CHILD-1 (jarvis-os/t_b24a9f07).

Run (terminal-capable seat / reviewer):
    python3 /home/frank/.hermes/agent-hooks/test_reconcile_referenced_done.py

Exit 0 = all assertions pass; non-zero = failure (prints which).

NOTE on the seed case: CHILD-1 verified t_7cca7076 -> t_349cf425 is now CLEARED
(both done). It is therefore exercised here as a *historical fixture* — we
reconstruct the pre-clear state (referencing lane open, referenced done) to
assert the hook produces the re-triage comment and the correct flip/reassign.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "reconcile-referenced-done.py"
spec = importlib.util.spec_from_file_location("reconcile_referenced_done", HOOK)
assert spec is not None and spec.loader is not None, f"cannot load {HOOK}"
hook = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hook
spec.loader.exec_module(hook)


def build_fixture() -> sqlite3.Connection:
    """In-memory board DB reproducing the seed case + the two live pairs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks(
            id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT,
            assignee TEXT, block_kind TEXT
        );
        CREATE TABLE task_comments(
            task_id TEXT, body TEXT, created_at INTEGER
        );
        CREATE TABLE task_links(
            parent_id TEXT, child_id TEXT
        );
        """
    )

    tasks = [
        # --- SEED CASE (historical reconstruction) -------------------------
        # Referenced source, done 2026-07-05 21:30.
        ("t_349cf425",
         "Investigate and fix Fusion Calibration report PnL calculation errors",
         "Fusion calibration PnL wrong.", "done", "builder", None),
        # Referencing lane: RESEARCH-ACTIONABLE, open (reconstructed pre-clear),
        # pure stale reference: no contrary verdict, no children, not human-gated.
        ("t_7cca7076",
         "RESEARCH-ACTIONABLE: jarvis-os/t_349cf425",
         "Track source jarvis-os/t_349cf425.", "blocked", "jarvis-os-pm", "transient"),

        # --- LIVE PAIR 1: t_fa2f4767 -> t_fd46da38 (genuine work: needs_input) ---
        ("t_fd46da38", "DURABLE GUARD: seeder enable", "guard", "done", "devops", None),
        ("t_fa2f4767",
         "REVIEW: t_fd46da38 durable seeder-enable guard (PR #479)",
         "Review t_fd46da38.", "blocked", "os-reviewer", "needs_input"),

        # --- LIVE PAIR 2: t_556e4d9b -> t_b6063f42 (Frank-gated: needs_input) ---
        ("t_b6063f42", "FIX: tournament evaluator dup routing", "fix", "done", "trading-devops", None),
        ("t_556e4d9b",
         "REVIEW+APPLY: corrected tournament-eval cron prompt (t_b6063f42 fix)",
         "Review t_b6063f42.", "blocked", "sycode-trading-pm", "needs_input"),

        # --- CONTRARY-VERDICT REVIEW lane (genuine work remains) ----------------
        ("t_dead0001", "Some referenced work", "x", "done", "builder", None),
        ("t_review01",
         "REVIEW: t_dead0001 some work",
         "Review t_dead0001.", "blocked", "os-reviewer", "transient"),

        # --- Lane with its own open child (NOT a pure stale reference) ----------
        ("t_dead0002", "Other referenced work", "x", "done", "builder", None),
        ("t_parent01",
         "RESEARCH-ACTIONABLE: t_dead0002",
         "Track t_dead0002.", "ready", "jarvis-os-pm", None),
        ("t_child001", "Sub work of t_parent01", "child", "todo", "builder", None),

        # --- NOT an auto-routed lane: merely mentions a done task -> untouched --
        ("t_dead0003", "Unrelated done task", "x", "done", "builder", None),
        ("t_build001",
         "Build feature referencing t_dead0003 for context",
         "Build lane, not a tracker.", "running", "builder", None),

        # --- dependency-blocked lane (human-gated, like needs_input) ------------
        ("t_dead0004", "Dependency-gated source", "x", "done", "builder", None),
        ("t_dep00001",
         "RESEARCH-ACTIONABLE: t_dead0004",
         "Track t_dead0004.", "blocked", "jarvis-os-pm", "dependency"),

        # --- reassign case with NO assignee (reassign_to=None edge) -------------
        ("t_dead0005", "Source with ownerless review lane", "x", "done", "builder", None),
        ("t_noown001",
         "REVIEW: t_dead0005 ownerless lane",
         "Review t_dead0005.", "blocked", None, "transient"),

        # --- COMPLETING-ID fixture: running task being completed, referencing lane open ---
        ("t_a1b2c3d4",
         "Task being completed right now",
         "Running but about to be done.", "running", "builder", None),
        ("t_ref_a1b2c3d4",
         "RESEARCH-ACTIONABLE: t_a1b2c3d4",
         "Track t_a1b2c3d4.", "blocked", "jarvis-os-pm", "transient"),
    ]
    conn.executemany(
        "INSERT INTO tasks VALUES(?,?,?,?,?,?)", tasks
    )
    comments = [
        # Contrary verdict on the REVIEW lane -> genuine work remains.
        ("t_review01", "REVIEW_VERDICT=CHANGES_REQUESTED: needs rework.", 100),
        # Contrary verdict on the ownerless lane -> forces the reassign branch.
        ("t_noown001", "REVIEW_VERDICT=REWORK_REQUIRED: owner unknown.", 100),
    ]
    conn.executemany("INSERT INTO task_comments VALUES(?,?,?)", comments)
    links = [("t_parent01", "t_child001")]
    conn.executemany("INSERT INTO task_links VALUES(?,?)", links)
    conn.commit()
    return conn


def classify_by_id(conn, hook, lane_id, ref_id):
    lane = hook.load_lane(conn, lane_id)
    assert lane is not None, f"fixture lane {lane_id} missing"
    return hook.classify_lane(lane, ref_id)


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = ""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    conn = build_fixture()

    # --- Acceptance #3 (seed case) -----------------------------------------
    a = classify_by_id(conn, hook, "t_7cca7076", "t_349cf425")
    check("seed: t_7cca7076 disposition == close", a.disposition == "close",
          f"got {a.disposition}")
    check("seed: comment has required re-triage phrasing",
          a.comment is not None
          and "referenced task t_349cf425 is DONE — re-triage" in a.comment,
          f"comment={a.comment!r}")

    # --- Live pair 1: needs_input must be comment-only (never auto-close) ---
    a = classify_by_id(conn, hook, "t_fa2f4767", "t_fd46da38")
    check("pair1: needs_input -> comment-only", a.disposition == "comment-only",
          f"got {a.disposition}")
    check("pair1: comment references done source",
          a.comment is not None and "t_fd46da38 is DONE" in a.comment)

    # --- Live pair 2: Frank-gated needs_input -> comment-only --------------
    a = classify_by_id(conn, hook, "t_556e4d9b", "t_b6063f42")
    check("pair2: needs_input -> comment-only", a.disposition == "comment-only",
          f"got {a.disposition}")

    # --- Contrary verdict REVIEW lane -> reassign to owner ------------------
    a = classify_by_id(conn, hook, "t_review01", "t_dead0001")
    check("contrary-verdict: reassign", a.disposition == "reassign",
          f"got {a.disposition}")
    check("contrary-verdict: reassign to owner", a.reassign_to == "os-reviewer",
          f"got {a.reassign_to}")

    # --- Lane with open child -> reassign (not pure stale ref) --------------
    a = classify_by_id(conn, hook, "t_parent01", "t_dead0002")
    check("open-child: reassign", a.disposition == "reassign",
          f"got {a.disposition}")

    # --- Completion-time discovery: find referencing lanes for a done id ----
    # Non-tracking lane (t_build001) must NOT be discovered as a referencing lane.
    lanes = hook.find_referencing_lanes(conn, "t_dead0003")
    check("discovery: non-tracking lane excluded",
          all(l.id != "t_build001" for l in lanes),
          f"discovered={[l.id for l in lanes]}")

    lanes = hook.find_referencing_lanes(conn, "t_349cf425")
    check("discovery: seed referencing lane found",
          any(l.id == "t_7cca7076" for l in lanes),
          f"discovered={[l.id for l in lanes]}")

    # --- dependency block_kind -> comment-only (human-gated) ----------------
    a = classify_by_id(conn, hook, "t_dep00001", "t_dead0004")
    check("dependency: comment-only (human-gated)", a.disposition == "comment-only",
          f"got {a.disposition}")

    # --- reassign with NO assignee: must not crash, reassign_to=None ----------
    a = classify_by_id(conn, hook, "t_noown001", "t_dead0005")
    check("no-assignee contrary: reassign", a.disposition == "reassign",
          f"got {a.disposition}")
    check("no-assignee contrary: reassign_to is None (no --assignee None call)",
          a.reassign_to is None, f"got {a.reassign_to!r}")

    # --- apply_action dry-run vs apply (subprocess stubbed) -------------------
    close_action = classify_by_id(conn, hook, "t_7cca7076", "t_349cf425")
    dry = hook.apply_action("jarvis-os", close_action, apply=False)
    check("apply_action dry-run: no mutation, WOULD prefix",
          dry.startswith("WOULD-CLOSE(dry-run)"), f"got {dry!r}")

    calls: list[list[str]] = []
    real_run = hook._run_hermes
    def stub_run(args, _calls=calls):
        _calls.append(list(args))
        return True, "ok"
    hook._run_hermes = stub_run
    try:
        line = hook.apply_action("jarvis-os", close_action, apply=True)
        check("apply_action apply: comment + complete issued",
              len(calls) == 2
              and any("comment" in c for c in calls[0])
              and any("complete" in c for c in calls[1]),
              f"calls={calls}")
        check("apply_action apply: no PARTIAL-FAILURE on success",
              "PARTIAL-FAILURE" not in line, f"line={line!r}")

        calls.clear()
        line = hook.apply_action("jarvis-os", a, apply=True)  # ownerless reassign
        check("apply_action no-assignee reassign: comment only, no update call",
              len(calls) == 1
              and any("comment" in c for c in calls[0]),
              f"calls={calls}")

        calls.clear()
        hook._run_hermes = lambda args: (False, "boom")
        line = hook.apply_action("jarvis-os", close_action, apply=True)
        check("apply_action apply: PARTIAL-FAILURE reported on subprocess failure",
              "PARTIAL-FAILURE" in line, f"line={line!r}")
    finally:
        hook._run_hermes = real_run

    # --- reconcile_on_done entry point (dry-run end-to-end) -------------------
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        tmp_db = tf.name
    try:
        disk = sqlite3.connect(tmp_db)
        conn.backup(disk)
        disk.close()
        lines = hook.reconcile_on_done(tmp_db, "jarvis-os", "t_349cf425", apply=False)
        check("reconcile_on_done: finds seed lane",
              any("t_7cca7076" in l and "WOULD-CLOSE" in l for l in lines),
              f"lines={lines}")
        # Idempotency signal: re-running against an already-commented lane
        # should still be safe/deterministic in dry-run (no duplicate close in
        # real apply is guarded by the completion gate invoking this once per
        # completion event; document the contract here).
        lines2 = hook.reconcile_on_done(tmp_db, "jarvis-os", "t_349cf425", apply=False)
        check("reconcile_on_done: deterministic across runs (dry-run)",
              lines == lines2, f"{lines} != {lines2}")
    finally:
        os.unlink(tmp_db)

    conn.close()

    # --- COMPLETING-ID mode: find lanes for a RUNNING task being completed -----
    conn2 = build_fixture()
    try:
        # Without completing_id, a running task is NOT found as a stale reference
        lanes = hook.find_referencing_lanes(conn2, "t_a1b2c3d4")
        check("completing-id: without completing_id, running task NOT found",
              all(l.id != "t_ref_a1b2c3d4" for l in lanes),
              f"discovered={[l.id for l in lanes]}")

        # WITH completing_id, the running task IS treated as done
        lanes = hook.find_referencing_lanes(conn2, "t_a1b2c3d4",
                                            completing_ids={"t_a1b2c3d4"})
        check("completing-id: with completing_id, running task IS found",
              any(l.id == "t_ref_a1b2c3d4" for l in lanes),
              f"discovered={[l.id for l in lanes]}")

        # reconcile_on_done with completing_id discovers and classifies the lane
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tmp_db = tf.name
        try:
            disk = sqlite3.connect(tmp_db)
            conn2.backup(disk)
            disk.close()
            lines = hook.reconcile_on_done(tmp_db, "jarvis-os", "t_a1b2c3d4",
                                           apply=False, completing_id="t_a1b2c3d4")
            check("completing-id: reconcile_on_done discovers lane",
                  any("t_ref_a1b2c3d4" in l and "WOULD-CLOSE" in l for l in lines),
                  f"lines={lines}")
            lines_no = hook.reconcile_on_done(tmp_db, "jarvis-os", "t_a1b2c3d4",
                                              apply=False)
            check("completing-id: without completing_id, reconcile finds nothing",
                  all("t_ref_a1b2c3d4" not in l for l in lines_no),
                  f"lines_no={lines_no}")
        finally:
            os.unlink(tmp_db)
    finally:
        conn2.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} assertion(s): {failures}")
        return 1
    print("ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
