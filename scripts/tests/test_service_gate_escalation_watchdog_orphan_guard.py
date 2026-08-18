#!/usr/bin/env python3
"""Verification harness for the service-gate watchdog orphan guard (t_3ab9e690).

Builds a synthetic KANBAN_DIR containing:
  - a LIVE source board with a genuinely blocked SERVICE-GATE task
  - a RETIRED board directory with NO kanban.db (startup self-check path)
  - a LIVE board whose escalated source task id has been deleted (orphan path)
  - the jarvis-os escalation board pre-seeded with an open escalation card
    pointing at each of the two dead sources

Asserts:
  1. startup self-check warns and skips the board dir with no kanban.db
  2. no NEW escalation is created for a non-existent board or task
  3. pre-existing orphan escalations are parked blocked/transient with exactly
     one explanatory comment, and are NOT re-commented on a second run
  4. the legitimate live source still escalates (no regression)
"""
import contextlib
import importlib.util
import io
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(
    "/home/frank/.hermes/scripts/service_gate_escalation_watchdog.py"
)

spec = importlib.util.spec_from_file_location("sgw", SCRIPT)
sgw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sgw)

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
OLD = NOW - (100 * 3600)  # well past the 6h threshold


def make_board(root: Path, slug: str) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return db


def add_task(db: Path, tid, title, status="blocked", block_kind="needs_input",
             created_by="pm", body="", created_at=OLD):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks (id,title,body,assignee,status,created_by,created_at,block_kind)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (tid, title, body, "worker", status, created_by, created_at, block_kind),
    )
    conn.execute(
        "INSERT INTO task_events (task_id,kind,created_at) VALUES (?,'blocked',?)",
        (tid, created_at),
    )
    conn.commit()
    conn.close()


def comments_for(db: Path, tid: str):
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id=?", (tid,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def status_of(db: Path, tid: str):
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    conn.close()
    return row


failures = []


def check(label, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        failures.append(label)
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "boards"
    root.mkdir(parents=True)
    sgw.KANBAN_DIR = root  # redirect the module at the synthetic fixture

    # --- fixtures ---
    esc_db = make_board(root, "jarvis-os")
    live_db = make_board(root, "live-board")
    ghost_db = make_board(root, "ghost-board")

    # retired board: directory exists, kanban.db does NOT
    (root / "retired-board").mkdir()

    # legitimate blocked source on the live board. Since t_b400dc8c, a bare
    # needs_input source is digest-routed — to keep exercising the real
    # escalation path (orphan guard must not silence genuine gates) this
    # fixture carries an explicit critical-R3 marker AND is fresh (<168h), so
    # it lands in the keep-set and still escalates.
    add_task(live_db, "t_live0001",
             "CRITICAL R3: SERVICE-GATE api key exposed cleartext")

    # ghost-board has a blocked task that WILL be escalated-looking, but the
    # escalation below points at an id that does not exist in it
    add_task(ghost_db, "t_other999", "SERVICE-GATE: unrelated", created_at=NOW)

    # pre-existing OPEN escalation -> source task deleted from a LIVE board
    add_task(
        esc_db, "t_orph0001",
        "FRANK ESCALATION: SERVICE-GATE task t_deleted1 blocked 106.8h",
        status="ready", block_kind=None, created_by="service-gate-escalation",
        body="**Source task:** t_deleted1 on board `ghost-board`\n",
    )
    # pre-existing OPEN escalation -> source BOARD has no kanban.db at all
    add_task(
        esc_db, "t_orph0002",
        "FRANK ESCALATION: SERVICE-GATE task t_deleted2 blocked 90.0h",
        status="ready", block_kind=None, created_by="service-gate-escalation",
        body="**Source task:** t_deleted2 on board `retired-board`\n",
    )

    def escalation_ids():
        conn = sqlite3.connect(str(esc_db))
        rows = conn.execute(
            "SELECT id FROM tasks WHERE created_by='service-gate-escalation'"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}

    before = escalation_ids()

    print("\n=== unit: existence primitives ===")
    check("board_db_exists('live-board') is True", sgw.board_db_exists("live-board"))
    check("board_db_exists('retired-board') is False",
          not sgw.board_db_exists("retired-board"))
    check("board_db_exists('never-existed') is False",
          not sgw.board_db_exists("never-existed"))
    check("task_exists live source", sgw.task_exists("live-board", "t_live0001"))
    check("task_exists deleted id is False",
          not sgw.task_exists("ghost-board", "t_deleted1"))
    check("task_exists on boardless board is False",
          not sgw.task_exists("retired-board", "t_deleted2"))
    ok, reason = sgw.source_exists("retired-board", "t_deleted2")
    check("source_exists reports missing board", not ok and "kanban.db" in reason, reason)
    ok, reason = sgw.source_exists("ghost-board", "t_deleted1")
    check("source_exists reports missing task", not ok and "not found" in reason, reason)

    print("\n=== unit: escalation source parsing ===")
    b, t = sgw.parse_escalation_source(
        "FRANK ESCALATION: SERVICE-GATE task t_a85ddbd9 blocked 106.8h",
        "**Source task:** t_a85ddbd9 on board `ai-restaurant`\n",
    )
    check("parses board+task from real card shape",
          (b, t) == ("ai-restaurant", "t_a85ddbd9"), f"{b}/{t}")

    print("\n=== criterion 4: startup self-check ===")
    live = sgw.check_configured_boards()
    check("retired-board excluded from scan", "retired-board" not in live, str(live))
    check("live boards retained",
          {"live-board", "ghost-board", "jarvis-os"} <= set(live), str(live))

    print("\n=== criteria 2+3: full run over synthetic fixture ===")
    sgw.main([])
    after = escalation_ids()
    new_cards = after - before

    # Every newly created card must point at a source that genuinely exists.
    conn = sqlite3.connect(str(esc_db))
    new_sources = {
        cid: sgw.parse_escalation_source(*conn.execute(
            "SELECT title, body FROM tasks WHERE id=?", (cid,)).fetchone())
        for cid in new_cards
    }
    conn.close()
    bad = {
        cid: (b, t) for cid, (b, t) in new_sources.items()
        if not sgw.source_exists(b or "", t or "")[0]
    }
    check("no NEW escalation created for a non-existent board/task",
          not bad, f"offending={bad}")
    check("no NEW escalation references the retired board",
          not any(b == "retired-board" for b, _ in new_sources.values()),
          str(new_sources))

    st1 = status_of(esc_db, "t_orph0001")
    check("orphan (deleted task) parked blocked/transient",
          st1 == ("blocked", "transient"), str(st1))
    st2 = status_of(esc_db, "t_orph0002")
    check("orphan (missing board db) parked blocked/transient",
          st2 == ("blocked", "transient"), str(st2))

    c1 = [c for c in comments_for(esc_db, "t_orph0001") if "ORPHANED SOURCE" in c]
    c2 = [c for c in comments_for(esc_db, "t_orph0002") if "ORPHANED SOURCE" in c]
    check("exactly one explanatory comment on orphan 1", len(c1) == 1, str(len(c1)))
    check("exactly one explanatory comment on orphan 2", len(c2) == 1, str(len(c2)))
    check("no heartbeat comment added to orphan 1",
          not any("re-fire heartbeat" in c for c in comments_for(esc_db, "t_orph0001")))

    print("\n=== no-regression: legitimate source still escalates ===")
    legit = [
        cid for cid in new_cards
        if "t_live0001" in sqlite3.connect(str(esc_db)).execute(
            "SELECT title FROM tasks WHERE id=?", (cid,)).fetchone()[0]
    ]
    check("live blocked source produced exactly one escalation",
          len(legit) == 1, f"new={sorted(new_cards)}")

    print("\n=== idempotency: second run must not re-comment orphans ===")
    sgw.main([])
    c1b = [c for c in comments_for(esc_db, "t_orph0001") if "ORPHANED SOURCE" in c]
    c2b = [c for c in comments_for(esc_db, "t_orph0002") if "ORPHANED SOURCE" in c]
    check("orphan 1 still has exactly one comment after re-run", len(c1b) == 1, str(len(c1b)))
    check("orphan 2 still has exactly one comment after re-run", len(c2b) == 1, str(len(c2b)))

# ---------------------------------------------------------------------------
# kanban t_4e8c2620: classification must run BEFORE the escalation rate limiter
# ---------------------------------------------------------------------------
# Under the old ordering the budget short-circuit ran first, so with the budget
# exhausted NO candidate reached source_exists() and `orphaned candidates
# skipped` read 0 regardless of how many orphans existed fleet-wide. These
# checks pin the census property and prove the budget still caps writes.

SUMMARY_RE = re.compile(
    r"(?P<escalated>\d+) escalated, "
    r"(?P<digest_routed>\d+) digest-routed \(needs_input -> weekly A3\), "
    r"(?P<kept_critical>\d+) kept-critical \(R3 keep-set\), "
    r"(?P<duplicate>\d+) duplicate \(heartbeated in place\), "
    r"(?P<recent>\d+) under threshold, "
    r"(?P<esc_tasks>\d+) escalation tasks skipped, "
    r"(?P<orphaned>\d+) orphaned candidates skipped, "
    r"(?P<parked>\d+) parked sources skipped, "
    r"(?P<retired>\d+) orphan escalations retired, "
    r"(?P<rate_limited>\d+) rate-limited, "
    r"(?P<boards>\d+) boards scanned, "
    r"(?P<reopened>\d+) reopened, "
    r"(?P<capped>\d+) capped"
)


def run_dry_summary() -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sgw.main(["--dry-run"])
    m = SUMMARY_RE.search(buf.getvalue())
    assert m, f"summary not parseable: {buf.getvalue()!r}"
    return {k: int(v) for k, v in m.groupdict().items()}


with tempfile.TemporaryDirectory() as tmp2:
    root2 = Path(tmp2) / "boards"
    root2.mkdir(parents=True)
    sgw.KANBAN_DIR = root2

    esc_db2 = make_board(root2, "jarvis-os")
    busy_db = make_board(root2, "busy-board")

    # Four RECENT watchdog escalations -> 24h budget fully consumed
    # (MAX_ESCALATIONS_PER_24H == 4). Each points at a live, non-blocked source
    # so reconcile_open_escalations() has nothing to retire.
    for i in range(1, 5):
        add_task(busy_db, f"t_keep000{i}", f"settled work {i}",
                 status="ready", block_kind=None, created_at=OLD)
        add_task(
            esc_db2, f"t_esc000{i}",
            f"FRANK ESCALATION: SERVICE-GATE task t_keep000{i} blocked 50.0h",
            status="ready", block_kind=None,
            created_by="service-gate-escalation",
            body=f"**Source task:** t_keep000{i} on board `busy-board`\n",
            created_at=NOW,
        )

    # Three genuine, long-blocked, escalation-eligible candidates. Since
    # t_b400dc8c these must carry a fresh critical-R3 marker to stay on the
    # escalation path (bare needs_input would be digest-routed before the rate
    # limiter, which would invalidate the classification-before-budget census
    # assertions below).
    for i in range(1, 4):
        add_task(busy_db, f"t_cand000{i}",
                 f"CRITICAL R3: SERVICE-GATE api key exposed cleartext {i}")

    # Two candidates whose source row vanished between the scan read and the
    # existence check (the retired-mid-run race the orphan guard exists for).
    real_get_blocked = sgw.get_blocked_tasks
    PHANTOMS = ["t_phantom1", "t_phantom2"]

    def get_blocked_with_phantoms(db_path):
        rows = real_get_blocked(db_path)
        if Path(db_path).parent.name == "busy-board":
            rows = rows + [
                {
                    "id": pid,
                    "title": f"SERVICE-GATE: vanished source {pid}",
                    "assignee": "worker",
                    "status": "blocked",
                    "block_kind": "needs_input",
                    "created_by": "pm",
                    "created_at": OLD,
                    "consecutive_failures": 0,
                    "last_failure_error": None,
                }
                for pid in PHANTOMS
            ]
        return rows

    sgw.get_blocked_tasks = get_blocked_with_phantoms
    try:
        print("\n=== unit: per-run existence cache ===")
        cache = sgw.SourceExistenceCache()
        check("cache agrees with uncached source_exists (live task)",
              cache.source_exists("busy-board", "t_cand0001")
              == sgw.source_exists("busy-board", "t_cand0001"))
        check("cache agrees with uncached source_exists (missing task)",
              cache.source_exists("busy-board", "t_phantom1")
              == sgw.source_exists("busy-board", "t_phantom1"))
        check("cache agrees with uncached source_exists (missing board)",
              cache.source_exists("no-such-board", "t_x")
              == sgw.source_exists("no-such-board", "t_x"))
        check("board_task_ids returns None for absent DB",
              sgw.board_task_ids("no-such-board") is None)
        check("board_task_ids enumerates live ids",
              {"t_cand0001", "t_cand0002", "t_cand0003"}
              <= (sgw.board_task_ids("busy-board") or set()))

        print("\n=== t_4e8c2620: orphan census with the budget exhausted ===")
        budget_used = sgw.recent_escalations_count(24)
        check("24h escalation budget is exhausted for this fixture",
              budget_used >= sgw.MAX_ESCALATIONS_PER_24H,
              f"recent={budget_used} max={sgw.MAX_ESCALATIONS_PER_24H}")

        s = run_dry_summary()
        check("all three candidates matched the critical-R3 keep-set",
              s["kept_critical"] == 3, str(s))
        check("orphaned candidates counted despite zero remaining budget",
              s["orphaned"] == len(PHANTOMS), str(s))
        check("no escalation reported with the budget exhausted",
              s["escalated"] == 0, str(s))
        check("eligible-but-unfundable candidates reported as rate-limited",
              s["rate_limited"] == 3, str(s))
        check("orphans are NOT double-counted as rate-limited",
              s["orphaned"] + s["rate_limited"] == 5, str(s))
        check("no orphan escalation retired in this fixture",
              s["retired"] == 0, str(s))

        print("\n=== t_4e8c2620: budget still caps writes (unchanged effect) ===")
        before2 = {
            r[0] for r in sqlite3.connect(str(esc_db2)).execute(
                "SELECT id FROM tasks WHERE created_by='service-gate-escalation'"
            ).fetchall()
        }
        sgw.main([])  # real run, budget exhausted
        after2 = {
            r[0] for r in sqlite3.connect(str(esc_db2)).execute(
                "SELECT id FROM tasks WHERE created_by='service-gate-escalation'"
            ).fetchall()
        }
        check("zero new escalation cards created when over budget",
              after2 == before2, f"new={sorted(after2 - before2)}")
        hb = [
            c for tid in [f"t_esc000{i}" for i in range(1, 5)]
            for c in comments_for(esc_db2, tid) if "re-fire heartbeat" in c
        ]
        check("no heartbeat written while over budget", not hb, str(len(hb)))

        print("\n=== t_4e8c2620: orphan census is budget-independent ===")
        # Free the budget by ageing the four seeded escalations out of the 24h
        # window. The orphan count must be identical to the exhausted-budget run.
        conn = sqlite3.connect(str(esc_db2))
        conn.execute(
            "UPDATE tasks SET created_at = ? "
            "WHERE created_by='service-gate-escalation'",
            (NOW - (48 * 3600),),
        )
        conn.commit()
        conn.close()
        check("24h budget is now free",
              sgw.recent_escalations_count(24) == 0,
              str(sgw.recent_escalations_count(24)))
        s2 = run_dry_summary()
        check("same orphan count with a full budget as with none",
              s2["orphaned"] == s["orphaned"], f"{s2} vs {s}")
        check("keep-set classification is budget-independent",
              s2["kept_critical"] == s["kept_critical"], f"{s2} vs {s}")
        check("with budget available exactly one escalation is reported",
              s2["escalated"] == sgw.MAX_ESCALATIONS_PER_RUN, str(s2))
        check("per-run cap still holds (remaining eligible are rate-limited)",
              s2["rate_limited"] == 3 - sgw.MAX_ESCALATIONS_PER_RUN, str(s2))
    finally:
        sgw.get_blocked_tasks = real_get_blocked

print()
if failures:
    print(f"RESULT: FAIL ({len(failures)} checks failed): {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
