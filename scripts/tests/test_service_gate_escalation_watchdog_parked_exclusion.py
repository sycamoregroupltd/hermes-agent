#!/usr/bin/env python3
"""Hermetic acceptance harness for the parked-source exclusion (kanban t_5956838b).

Defect: the source task ai-restaurant/t_a85ddbd9 was parked as
'awaiting-absent-seat' (orchestrator disposition, jarvis seat 2026-08-03) with
the canonical comment marker ``PARKED: awaiting-absent-seat``, but the live
watchdog had NO exclusion for that state — a prior PM completion (2026-08-04)
claimed the exclusion was implemented, but the shipped script (mtime 2026-08-01,
before that claim) never contained it. Result: the watchdog kept matching the
parked source every 30 minutes, and its time-aware dedupe (kanban t_9a621399)
reopened the resolved escalation card instead of staying silent — pure noise,
no new information.

This harness is HERMETIC. It builds temp board DBs under a temp KANBAN_DIR and
never touches a live board. It loads the CURRENT live script and asserts the
exclusion works in BOTH directions (updated for kanban t_b400dc8c digest
routing):

  A  parked source suppressed  a blocked SERVICE-GATE source carrying the
                              'PARKED: awaiting-absent-seat' comment marker is
                              skipped and counted, and NO escalation card is
                              created/heartbeated/reopened for it.
  B  stalled source digest-routed  a blocked SERVICE-GATE source WITHOUT the
                              parked marker is no longer per-task escalated:
                              since t_b400dc8c a bare needs_input source is
                              routed to the weekly Frank A3 digest, never to a
                              FRANK ESCALATION card.
  B2 keep-set still fires    a blocked SERVICE-GATE source carrying a FRESH
                              critical-R3 marker (real-time security/credential
                              incident, block < 168h) STILL escalates in the
                              same run — the digest routing must never silence
                              a genuine critical R3 gate.

Also asserts:
  C  raw query unfiltered   get_blocked_tasks() still returns the parked
                              source (the exclusion lives in main's loop, so
                              the counter proves it fired, not that the query
                              silently dropped the row).
  D  fail-safe schema       a board whose DB lacks a task_comments table
                              yields an empty parked set (no suppression), so
                              an unreadable comments table cannot silently
                              silence genuine escalations.
  E  master-orchestrator park a blocked SERVICE-GATE source carrying the
                              'PARKED-BY-MASTER-ORCHESTRATOR ... DO-NOT-AUTO-
                              UNBLOCK' marker (master-orchestrator load-shed
                              park, kanban t_7024c661) is skipped and counted
                              under skipped_parked, and NO escalation/heartbeat/
                              reopen card is created for it.
"""
import contextlib
import glob
import importlib.machinery
import importlib.util
import io
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path("/home/frank/.hermes/profiles/jarvis/scripts")
LIVE = Path("/home/frank/.hermes/scripts/service_gate_escalation_watchdog.py")

# Live tasks schema fields this script actually touches.
SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT,
    assignee TEXT,
    status TEXT NOT NULL,
    priority INTEGER DEFAULT 0,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch',
    block_kind TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_error TEXT,
    last_heartbeat_at INTEGER
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
"""

NOW = int(time.time())
T0 = NOW - (100 * 3600)          # source blocked 100h ago, well past 6h gate

failures = []


def check(label, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(label)
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None, f"could not build a module spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def make_board(root: Path, slug: str) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db


def add_blocked_source(db: Path, tid: str, title: str, parked: bool):
    """Blocked SERVICE-GATE source task, optionally carrying the parked marker."""
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, "
        "created_by, created_at, block_kind, consecutive_failures) "
        "VALUES (?, ?, ?, ?, 'blocked', 3, 'tester', ?, 'needs_input', 0)",
        (tid, title, f"Source task {tid} body", "capability-builder", T0),
    )
    if parked:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'capability-builder', "
            "'PARKED: awaiting-absent-seat — waiting on a seat that does not "
            "exist on this host', ?)",
            (tid, NOW - 3600),
        )
    conn.commit()
    conn.close()


def add_master_parked_source(db: Path, tid: str, title: str):
    """Blocked SERVICE-GATE source task parked by master-orchestrator load-shed.

    Carries the ``PARKED-BY-MASTER-ORCHESTRATOR ... DO-NOT-AUTO-UNBLOCK``
    comment marker (kanban t_7024c661) — must be skipped and counted under
    skipped_parked, and must NEVER auto-escalate/reopen a card.
    """
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, "
        "created_by, created_at, block_kind, consecutive_failures) "
        "VALUES (?, ?, ?, ?, 'blocked', 3, 'tester', ?, 'needs_input', 0)",
        (tid, title, f"Source task {tid} body", "capability-builder", T0),
    )
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, 'master-orchestrator', "
        "'PARKED-BY-MASTER-ORCHESTRATOR dgx-hermes-orchestrator-71 load-shed "
        "DO-NOT-AUTO-UNBLOCK. Release only by master decision. Reversible: "
        "unblock.', ?)",
        (tid, NOW - 3600),
    )
    conn.commit()
    conn.close()


def build_fixture(root: Path, src_id: str, parked: bool, other_src_id: str):
    make_board(root, "jarvis-os")
    src_board = root / "ai-restaurant"
    src_db = make_board(root, "ai-restaurant")
    add_blocked_source(src_db, src_id, "SERVICE-GATE parked source", parked)
    add_blocked_source(
        src_db,
        other_src_id,
        "SERVICE-GATE stalled source (reachable owner)",
        False,
    )


def build_critical_fixture(root: Path, src_id: str):
    """Board with one FRESH critical-R3 source (keep-set) and no parked source."""
    make_board(root, "jarvis-os")
    src_db = make_board(root, "ai-restaurant")
    add_blocked_source(
        src_db,
        src_id,
        "CRITICAL R3: SERVICE-GATE api key exposed cleartext",
        False,
    )


def build_master_parked_fixture(root: Path, src_id: str):
    """Board with one master-orchestrator load-shed parked source (t_7024c661)."""
    make_board(root, "jarvis-os")
    src_db = make_board(root, "ai-restaurant")
    add_master_parked_source(
        src_db,
        src_id,
        "SERVICE-GATE master load-shed parked source",
    )


def main():
    assert LIVE.is_file(), f"live watchdog not found: {LIVE}"
    mod = load(LIVE, "watchdog_live")

    print("=== Scenario A+B: parked suppressed, stalled digest-routed (same run) ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "boards"
        build_fixture(root, "t_parked001", parked=True, other_src_id="t_stalled001")
        mod.KANBAN_DIR = root
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            mod.main(["--dry-run"])
        out = buf.getvalue() + "\n" + err.getvalue()
        check(
            "A: parked source skipped and counted",
            "SKIP parked source ai-restaurant/t_parked001" in err.getvalue()
            and "1 parked sources skipped" in buf.getvalue(),
            repr(err.getvalue()),
        )
        check(
            "B: stalled (non-parked) needs_input source is digest-routed, not escalated",
            "DIGEST-DECISION ai-restaurant/t_stalled001 keep=False" in err.getvalue()
            and "1 digest-routed" in buf.getvalue()
            and "DRY-RUN would ESCALATE: ai-restaurant/t_stalled001"
            not in err.getvalue(),
            repr(err.getvalue()),
        )
        check(
            "A/B: no escalation card created for parked source",
            mod.get_parked_source_ids(root / "ai-restaurant" / "kanban.db")
            == {"t_parked001"},
            repr(mod.get_parked_source_ids(root / "ai-restaurant" / "kanban.db")),
        )

    print("=== Scenario B2: fresh critical-R3 source still escalates (keep-set) ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "boards"
        build_critical_fixture(root, "t_crit001")
        mod.KANBAN_DIR = root
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            mod.main(["--dry-run"])
        check(
            "B2: critical-R3 source escalates immediately (kept, not digest-routed)",
            "DIGEST-DECISION ai-restaurant/t_crit001 keep=True" in err.getvalue()
            and "DRY-RUN would ESCALATE: ai-restaurant/t_crit001" in err.getvalue()
            and "1 escalated" in buf.getvalue()
            and "1 kept-critical" in buf.getvalue(),
            repr(err.getvalue() + "\n" + buf.getvalue()),
        )

    print("=== Scenario C: raw query is unfiltered (loop owns the exclusion) ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "boards"
        build_fixture(root, "t_parked002", parked=True, other_src_id="t_stalled002")
        raw = mod.get_blocked_tasks(root / "ai-restaurant" / "kanban.db")
        check(
            "C: get_blocked_tasks returns the parked source too",
            any(r["id"] == "t_parked002" for r in raw)
            and any(r["id"] == "t_stalled002" for r in raw),
            repr([r["id"] for r in raw]),
        )

    print("=== Scenario D: missing task_comments table fails safe ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "boards"
        # Build a board WITHOUT a task_comments table.
        d = root / "bare-board"
        d.mkdir(parents=True, exist_ok=True)
        db = d / "kanban.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "status TEXT NOT NULL, block_kind TEXT, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO tasks VALUES ('t_bare001', 'SERVICE-GATE bare source', "
            "'blocked', 'needs_input', ?)",
            (T0,),
        )
        conn.commit()
        conn.close()
        parked = mod.get_parked_source_ids(db)
        check(
            "D: schema-less board yields empty parked set (no suppression)",
            parked == set(),
            repr(parked),
        )

    print("=== Scenario E: master-orchestrator parked source skipped, never escalated ===")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "boards"
        build_master_parked_fixture(root, "t_masterpark001")
        mod.KANBAN_DIR = root
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            mod.main(["--dry-run"])
        check(
            "E: master-orchestrator parked source skipped and counted",
            "SKIP parked source ai-restaurant/t_masterpark001: "
            "master-orchestrator load-shed" in err.getvalue()
            and "1 parked sources skipped" in buf.getvalue(),
            repr(err.getvalue() + "\n" + buf.getvalue()),
        )
        check(
            "E: no escalation/reopen for master-parked source",
            "DRY-RUN would ESCALATE: ai-restaurant/t_masterpark001"
            not in err.getvalue(),
            repr(err.getvalue()),
        )
        check(
            "E: get_parked_source_ids includes master-parked source",
            mod.get_parked_source_ids(root / "ai-restaurant" / "kanban.db")
            == {"t_masterpark001"},
            repr(mod.get_parked_source_ids(root / "ai-restaurant" / "kanban.db")),
        )

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} checks failed): {failures}")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
