#!/usr/bin/env python3
"""Side-effect-free regression tests for kanban_dedupe_guard.py RULE 6
(blocked-reference cooldown, t_69764ac8).

Covers the five required scenarios from the accepted design (t_69764ac8
body, "REQUIRED before merge/enforcement" #1):
  1. unchanged-signature-within-TTL -> suppress+reblock
  2. changed-signature -> dispatch normally
  3. ref done/archived/unblocked -> dispatch normally
  4. credential/approval-critical marker -> never suppressed
  5. TTL elapsed -> exactly one fresh dispatch then re-suppress

No live board mutation: run_hermes() is monkeypatched to a no-op recorder,
and every board lives under a tempdir BOARDS_DIR redirect.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "kanban_dedupe_guard.py"
SPEC = importlib.util.spec_from_file_location("kanban_dedupe_guard", SCRIPT)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT,
    assignee TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    block_kind TEXT
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL
);
"""

NOW = int(time.time())

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(label)
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")


def make_board(root: Path, slug: str) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db


def add_task(db: Path, tid: str, title: str, *, status: str = "ready",
             body: str = "", block_kind: str | None = None,
             created_at: int = NOW) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks (id,title,body,assignee,status,created_at,block_kind)"
        " VALUES (?,?,?,?,?,?,?)",
        (tid, title, body, "devops", status, created_at, block_kind),
    )
    conn.commit()
    conn.close()


def add_comment(db: Path, tid: str, body: str, created_at: int = NOW) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,?)",
        (tid, "devops", body, created_at),
    )
    conn.commit()
    conn.close()


def status_of(db: Path, tid: str):
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.close()
    return row


class RunHermesRecorder:
    """Records every hermes CLI invocation the guard would have made,
    without ever spawning a subprocess or mutating a real board."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> bool:
        self.calls.append(args)
        return True


def run_scan(board: str, *, dry_run: bool, enforce: bool, state: dict) -> list[str]:
    data = guard.load_board(board)
    guard._BOARD_CACHE[board] = data
    report: list[str] = []
    guard.scan_board_blocked_ref_cooldown(
        board, state=state, dry_run=dry_run, enforce=enforce, report=report,
        tasks=data["tasks"], comments=data["comments"],
    )
    return report


# ---------------------------------------------------------------------------
# Scenario 1: unchanged-signature-within-TTL -> suppress+reblock
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "boards"
    root.mkdir(parents=True)
    guard.BOARDS_DIR = root
    guard._BOARD_CACHE.clear()
    recorder = RunHermesRecorder()
    guard.run_hermes = recorder

    src_db = make_board(root, "src-board")
    ref_db = make_board(root, "ref-board")

    add_task(
        src_db, "t_5000a001",
        "LAND PR (blocked)",
        status="ready",
        body="RESUME_GATE: waiting on t_ee000001 for the merge-gate decision.",
    )
    add_task(
        ref_db, "t_ee000001", "os-architect: merge-gate decision pending",
        status="blocked", body="Design packet on disk, awaiting Frank ruling.",
    )

    state = {"actions": {}}
    print("\n=== scenario 1a: first sighting is a BASELINE, never suppressed ===")
    r1 = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("first pass reports BASELINE, not SUPPRESS",
          any("BASELINE" in line for line in r1) and not any("SUPPRESS" in line for line in r1),
          str(r1))
    check("no hermes CLI call fired on first sighting", recorder.calls == [], str(recorder.calls))
    check("baseline recorded in state", any(k.startswith(guard.RULE6_KEY_PREFIX) for k in state["actions"]))

    print("\n=== scenario 1b: second sighting, unchanged sig, within TTL -> SUPPRESS+reblock ===")
    recorder.calls.clear()
    r2 = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("second pass reports SUPPRESS", any("SUPPRESS" in line and "WOULD-SUPPRESS" not in line for line in r2), str(r2))
    check("reblock (kind=dependency) CLI call fired exactly once", len(recorder.calls) == 1, str(recorder.calls))
    if recorder.calls:
        call = recorder.calls[0]
        check("reblock call targets the correct task", "t_5000a001" in call, str(call))
        check("reblock call uses kind=dependency", call[-2:] == ["--kind", "dependency"], str(call))

    print("\n=== scenario 1c: dry-run never calls hermes CLI even on a match ===")
    recorder.calls.clear()
    r3 = run_scan("src-board", dry_run=True, enforce=True, state=dict(state))
    check("dry-run reports WOULD-SUPPRESS", any("WOULD-SUPPRESS" in line for line in r3), str(r3))
    check("dry-run never calls hermes CLI", recorder.calls == [], str(recorder.calls))

    print("\n=== scenario 1d: enforce=False (default) never calls hermes CLI ===")
    recorder.calls.clear()
    r4 = run_scan("src-board", dry_run=False, enforce=False, state=dict(state))
    check("report-only mode reports SKIPPED", any("SKIPPED" in line for line in r4), str(r4))
    check("report-only mode never calls hermes CLI", recorder.calls == [], str(recorder.calls))


# ---------------------------------------------------------------------------
# Scenario 2: changed-signature -> dispatch normally (no suppression)
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "boards"
    root.mkdir(parents=True)
    guard.BOARDS_DIR = root
    guard._BOARD_CACHE.clear()
    recorder = RunHermesRecorder()
    guard.run_hermes = recorder

    src_db = make_board(root, "src-board")
    ref_db = make_board(root, "ref-board")

    add_task(
        src_db, "t_5000a002", "LAND PR (blocked)", status="ready",
        body="RESUME_GATE: waiting on t_ee000002 for the merge-gate decision.",
    )
    add_task(
        ref_db, "t_ee000002", "os-architect: merge-gate decision pending",
        status="blocked", body="Design packet on disk, awaiting Frank ruling.",
    )

    state = {"actions": {}}
    print("\n=== scenario 2: baseline then ref content changes -> no suppression ===")
    run_scan("src-board", dry_run=False, enforce=True, state=state)  # baseline
    # Mutate the referenced task's body (content signature changes).
    conn = sqlite3.connect(str(ref_db))
    conn.execute(
        "UPDATE tasks SET body=? WHERE id='t_ee000002'",
        ("Design packet on disk, Frank RULED: proceed with option B.",),
    )
    conn.commit()
    conn.close()
    guard._BOARD_CACHE.clear()
    recorder.calls.clear()
    r = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("changed ref signature produces a fresh BASELINE, not SUPPRESS",
          any("BASELINE" in line for line in r) and not any("SUPPRESS" in line and "WOULD" not in line for line in r),
          str(r))
    check("no reblock CLI call fired when signature changed", recorder.calls == [], str(recorder.calls))


# ---------------------------------------------------------------------------
# Scenario 3: ref done/archived/unblocked -> dispatch normally
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "boards"
    root.mkdir(parents=True)
    guard.BOARDS_DIR = root
    guard._BOARD_CACHE.clear()
    recorder = RunHermesRecorder()
    guard.run_hermes = recorder

    src_db = make_board(root, "src-board")
    ref_db = make_board(root, "ref-board")

    add_task(
        src_db, "t_5000a003", "LAND PR (blocked)", status="ready",
        body="RESUME_GATE: waiting on t_ee000003 for the merge-gate decision.",
    )
    add_task(ref_db, "t_ee000003", "os-architect: merge-gate decision", status="done")

    state = {"actions": {}}
    print("\n=== scenario 3: referenced task is done -> never matches RULE 6 ===")
    r = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("no RULE6 finding at all for a done reference", not r, str(r))
    check("no state written for a done reference", state["actions"] == {}, str(state["actions"]))


# ---------------------------------------------------------------------------
# Scenario 4: credential/approval-critical marker -> never suppressed
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "boards"
    root.mkdir(parents=True)
    guard.BOARDS_DIR = root
    guard._BOARD_CACHE.clear()
    recorder = RunHermesRecorder()
    guard.run_hermes = recorder

    src_db = make_board(root, "src-board")
    ref_db = make_board(root, "ref-board")

    add_task(
        src_db, "t_5000a004",
        "LAND PR (blocked) — needs credential rotation",
        status="ready",
        body="RESUME_GATE: waiting on t_ee000004 for the merge-gate decision. "
             "Needs approval for a production deploy credential rotation.",
    )
    add_task(
        ref_db, "t_ee000004", "os-architect: merge-gate decision pending",
        status="blocked", body="Design packet on disk, awaiting Frank ruling.",
    )

    state = {"actions": {}}
    print("\n=== scenario 4: credential/approval marker -> never suppressed, any pass ===")
    r1 = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("critical marker -> no RULE6 finding on first pass", not r1, str(r1))
    r2 = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("critical marker -> still no RULE6 finding on repeat pass", not r2, str(r2))
    check("no state ever written for a critical-marker candidate",
          state["actions"] == {}, str(state["actions"]))
    check("no hermes CLI call ever fired for a critical-marker candidate",
          recorder.calls == [], str(recorder.calls))


# ---------------------------------------------------------------------------
# Scenario 5: TTL elapsed -> exactly one fresh dispatch then re-suppress
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "boards"
    root.mkdir(parents=True)
    guard.BOARDS_DIR = root
    guard._BOARD_CACHE.clear()
    recorder = RunHermesRecorder()
    guard.run_hermes = recorder

    src_db = make_board(root, "src-board")
    ref_db = make_board(root, "ref-board")

    add_task(
        src_db, "t_5000a005", "LAND PR (blocked)", status="ready",
        body="RESUME_GATE: waiting on t_ee000005 for the merge-gate decision.",
    )
    add_task(
        ref_db, "t_ee000005", "os-architect: merge-gate decision pending",
        status="blocked", body="Design packet on disk, awaiting Frank ruling.",
    )

    state = {"actions": {}}
    print("\n=== scenario 5: TTL elapsed -> one fresh dispatch, then resume suppressing ===")
    run_scan("src-board", dry_run=False, enforce=True, state=state)  # baseline
    # Age the recorded baseline past the TTL.
    key = next(k for k in state["actions"] if k.startswith(guard.RULE6_KEY_PREFIX))
    expired = time.strftime(
        guard.RULE6_ISO_FMT,
        time.gmtime(time.time() - (guard.RULE6_TTL_HOURS + 1) * 3600),
    )
    state["actions"][key] = expired

    recorder.calls.clear()
    r_ttl = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("TTL-elapsed pass reports TTL-REFRESH, not SUPPRESS",
          any("TTL-REFRESH" in line for line in r_ttl) and not any("SUPPRESS" in line and "WOULD" not in line for line in r_ttl),
          str(r_ttl))
    check("no reblock CLI call fired on the TTL-refresh pass", recorder.calls == [], str(recorder.calls))
    check("state timestamp refreshed to now (not still expired)",
          state["actions"][key] != expired, str(state["actions"][key]))

    print("\n=== scenario 5b: immediately after TTL refresh, resumes suppressing ===")
    recorder.calls.clear()
    r_resume = run_scan("src-board", dry_run=False, enforce=True, state=state)
    check("post-refresh pass suppresses again",
          any("SUPPRESS" in line and "WOULD" not in line for line in r_resume), str(r_resume))
    check("reblock CLI call fires again after refresh", len(recorder.calls) == 1, str(recorder.calls))


print()
if failures:
    print(f"RESULT: FAIL ({len(failures)} checks failed): {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
