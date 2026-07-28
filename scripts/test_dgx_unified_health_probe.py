#!/usr/bin/env python3
"""Regression tests for dgx_unified_health_probe.py.

Covers:
  * Repeat-BLOCK hard-alert escalation (t_7a97ba51 #3 / t_cafc1119 C3):
    - critical_alert_due() returns False below the threshold and True at/above
      CRITICAL_ALERT_MIN_COUNT (>=3) within CRITICAL_ALERT_WINDOW_H.
    - events older than the window do NOT count (rolling-window correctness).
    - record_block_event() is append-only and idempotent per call.
  * check_kanban_crashes() precision:
    - crashed/gave_up run on a still-active (running) task counts as ACTIVE.
    - same run on a resolved (done) task counts as STALE (not active).
    - de-dupes multiple runs of one task to a single active hit.

No live board is touched; everything uses temp dirs. The canary module is
imported with its path constant monkeypatched to the temp layout.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "dgx_unified_health_probe.py"

spec = importlib.util.spec_from_file_location("uhealth", MODULE_PATH)
assert spec is not None and spec.loader is not None
uhealth: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uhealth)


def _now_iso(offset_min: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_min)


def test_repeat_escalation_below_threshold():
    tmp = Path(tempfile.mkdtemp())
    state = tmp / "blocks.jsonl"
    uhealth.CRITICAL_ALERT_STATE = state
    # Seed 2 events within window -> below threshold (>=3)
    with state.open("w", encoding="utf-8") as fh:
        for i in range(2):
            fh.write(json.dumps({"ts": _now_iso(-i).isoformat()}) + "\n")
    # A fresh BLOCK recorded now = 3rd event total -> still need >=3 counts
    # AFTER recording. critical_alert_due records the current event then counts.
    due = uhealth.critical_alert_due(_now_iso())
    # 2 seeded + 1 current = 3 >= 3 => due True. Use 1 seed to test below.
    assert due is True, "3 events in window should be due"

    # Now prove strictly-below: clear and seed 1
    state.write_text(json.dumps({"ts": _now_iso(0).isoformat()}) + "\n")
    # critical_alert_due will add a 2nd => total 2 < 3 => not due
    due2 = uhealth.critical_alert_due(_now_iso())
    assert due2 is False, "2 events in window should NOT be due"


def test_window_rolloff_excludes_old_events():
    tmp = Path(tempfile.mkdtemp())
    state = tmp / "blocks.jsonl"
    uhealth.CRITICAL_ALERT_STATE = state
    # 5 events, all older than the window (window default 24h, push 25h back)
    old = _now_iso(-(uhealth.CRITICAL_ALERT_WINDOW_H * 60 + 30))
    with state.open("w", encoding="utf-8") as fh:
        for _ in range(5):
            fh.write(json.dumps({"ts": old.isoformat()}) + "\n")
    # current event + 5 ancient = only 1 in window -> not due
    due = uhealth.critical_alert_due(_now_iso())
    assert due is False, "ancient events must roll off the window"


def test_record_block_event_append_only_and_count():
    tmp = Path(tempfile.mkdtemp())
    state = tmp / "blocks.jsonl"
    uhealth.CRITICAL_ALERT_STATE = state
    assert not state.exists()
    n1 = uhealth.record_block_event(_now_iso())
    assert state.exists()
    lines = state.read_text().splitlines()
    assert len(lines) == 1
    assert n1 == 1
    # second call appends (no overwrite), count increments
    n2 = uhealth.record_block_event(_now_iso())
    assert len(state.read_text().splitlines()) == 2
    assert n2 == 2


def _make_board(board_dir: Path, task_id: str, task_status: str,
                outcome: str, ended_offset_min: float,
                parent_status: str | None = None) -> None:
    board_dir.mkdir(parents=True, exist_ok=True)
    db = board_dir / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS task_links (parent_id TEXT, child_id TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS task_runs "
                "(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER)")
    con.execute("INSERT OR REPLACE INTO tasks (id, status) VALUES (?, ?)",
                (task_id, task_status))
    if parent_status is not None:
        parent_id = f"{task_id}_parent"
        con.execute("INSERT OR REPLACE INTO tasks (id, status) VALUES (?, ?)",
                    (parent_id, parent_status))
        con.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (parent_id, task_id))
    ended = int(_now_iso(ended_offset_min).timestamp())
    con.execute("INSERT INTO task_runs (task_id, outcome, ended_at) VALUES (?, ?, ?)",
                (task_id, outcome, ended))
    con.commit()
    con.close()


def _make_ready_backlog_db(db: Path, now: datetime) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, created_at INTEGER)"
    )
    rows = [
        ("t_old_devops", "old devops", "devops", "ready", int((now - timedelta(days=10)).timestamp())),
        ("t_mid_builder", "builder", "builder", "ready", int((now - timedelta(days=6)).timestamp())),
        ("t_new_devops", "new devops", "devops", "ready", int((now - timedelta(days=2)).timestamp())),
        ("t_running_devops", "running", "devops", "running", int((now - timedelta(days=20)).timestamp())),
        ("t_blocked_old", "blocked", "devops", "blocked", int((now - timedelta(days=30)).timestamp())),
    ]
    con.executemany(
        "INSERT INTO tasks (id, title, assignee, status, created_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def test_check_kanban_crashes_active_vs_stale():
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    # Active crash: running task with crashed run in window
    _make_board(boards / "jarvis-os", "t_active", "running", "crashed", -5)
    # Stale crash: done task with crashed run in window
    _make_board(boards / "sycode-trading", "t_done", "done", "gave_up", -5)
    uhealth.BOARDS_DIR = boards
    count, hits, stale = uhealth.check_kanban_crashes()
    assert count == 1, f"expected 1 active crash, got {count}: {hits}"
    assert any("t_active" in h for h in hits), f"active hit missing: {hits}"
    assert stale == 1, f"expected 1 stale, got {stale}"
    assert not any("t_done" in h for h in hits), f"stale leaked into active: {hits}"


def test_jarvis_ready_backlog_counts_and_ages():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    db = tmp / "jarvis-os" / "kanban.db"
    _make_ready_backlog_db(db, now)
    uhealth.JARVIS_OS_KANBAN_DB = db

    rep = uhealth.check_jarvis_ready_backlog(now)
    assert rep["available"] is True
    assert rep["ready_total"] == 3
    assert rep["oldest_ready_age_days"] == 10.0
    assert rep["devops_ready_count"] == 2
    assert rep["oldest_devops_ready_age_days"] == 10.0
    assert rep["top_ready_ids"] == ["t_old_devops", "t_mid_builder", "t_new_devops"]
    assert rep["top_devops_ready_ids"] == ["t_old_devops", "t_new_devops"]


def test_jarvis_ready_backlog_observability_does_not_block_main():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    db = tmp / "jarvis-os" / "kanban.db"
    _make_ready_backlog_db(db, now)

    uhealth.JARVIS_OS_KANBAN_DB = db
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.check_hermes_cli = lambda: (True, "ok", False)
    uhealth.check_gateway_unit = lambda: (True, "ok", False)
    uhealth.check_gateway_runtime = lambda: (True, True, "ok")
    uhealth.check_cron_ticker = lambda: (True, "ok", False)
    uhealth.check_canary_freshness = lambda: (True, "ok")
    uhealth.check_docker = lambda: (True, "ok", False)
    uhealth.check_disk = lambda: (True, "ok", False)
    uhealth.check_mechanism_matrix = lambda: {
        "available": True,
        "overall": "GREEN",
        "dead": 0,
        "detail": "ok",
        "fork_resource_pressure": False,
    }
    uhealth.check_kanban_crashes = lambda: (0, [], 0)
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verdict"] == "PASS"
    assert record["jarvis_ready_backlog"]["oldest_ready_age_days"] == 10.0


def test_legacy_substrate_bridge_stamps_fresh_health_canary_record():
    # t_bd9d284e: the unified probe must re-stamp a fresh substrate record
    # into the legacy health_canary.jsonl every cycle so legacy consumers
    # (dgx_report_anomaly_detector.py, t_5311fb77 channel, spine-audit) never
    # read the frozen ~24h-stale gateway_running left by the paused canary.
    tmp = Path(tempfile.mkdtemp())
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.check_hermes_cli = lambda: (True, "ok", False)
    uhealth.check_gateway_unit = lambda: (True, "ok", False)
    uhealth.check_gateway_runtime = lambda: (True, True, "ok")
    uhealth.check_cron_ticker = lambda: (True, "ok", False)
    uhealth.check_canary_freshness = lambda: (True, "ok")
    uhealth.check_docker = lambda: (True, "ok", False)
    uhealth.check_disk = lambda: (True, "ok", False)
    uhealth.check_mechanism_matrix = lambda: {
        "available": True,
        "overall": "GREEN",
        "dead": 0,
        "detail": "ok",
        "fork_resource_pressure": False,
    }
    uhealth.check_kanban_crashes = lambda: (0, [], 0)
    uhealth.utc_now = lambda: datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc)

    rc = uhealth.main()
    assert rc == 0
    legacy = uhealth.CRON_OUTPUT / "health_canary.jsonl"
    assert legacy.exists(), "bridge must write legacy health_canary.jsonl"
    lines = legacy.read_text(encoding="utf-8").splitlines()
    assert lines, "bridge wrote at least one record"
    rec = json.loads(lines[-1])
    assert rec.get("substrate_source") == "unified-health-probe"
    assert rec.get("hermes_cli") is True
    assert rec.get("gateway_running") is True
    assert rec.get("verdict") == "PASS"


def test_check_kanban_crashes_treats_done_parent_active_child_as_stale():
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    _make_board(
        boards / "jarvis-os",
        "t_done_parent_child",
        "running",
        "gave_up",
        -5,
        parent_status="done",
    )
    _make_board(boards / "sycode-trading", "t_unparented", "running", "crashed", -5)
    uhealth.BOARDS_DIR = boards
    count, hits, stale = uhealth.check_kanban_crashes()
    assert count == 1, f"expected only unparented active crash, got {count}: {hits}"
    assert any("t_unparented" in h for h in hits), f"active hit missing: {hits}"
    assert not any("t_done_parent_child" in h for h in hits), \
        f"done-parent child leaked into active hits: {hits}"
    assert stale == 1, f"expected done-parent child stale count, got {stale}"


def test_check_kanban_crashes_dedupes_same_task():
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    bd = boards / "jarvis-os"
    bd.mkdir(parents=True, exist_ok=True)
    db = bd / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    con.execute("CREATE TABLE task_runs "
                "(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER)")
    con.execute("INSERT INTO tasks VALUES ('t_x', 'ready')")
    ended = int(_now_iso(-3).timestamp())
    for i in range(4):  # 4 crashed runs for the SAME task
        con.execute("INSERT INTO task_runs (task_id, outcome, ended_at) "
                    "VALUES ('t_x', 'crashed', ?)", (ended,))
    con.commit()
    con.close()
    uhealth.BOARDS_DIR = boards
    count, hits, stale = uhealth.check_kanban_crashes()
    assert count == 1, f"same-task multi-run should dedup to 1, got {count}: {hits}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
