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

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "dgx_unified_health_probe.py"

spec = importlib.util.spec_from_file_location("uhealth", MODULE_PATH)
uhealth = importlib.util.module_from_spec(spec)
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
                outcome: str, ended_offset_min: float) -> None:
    board_dir.mkdir(parents=True, exist_ok=True)
    db = board_dir / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS task_runs "
                "(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER)")
    con.execute("INSERT OR REPLACE INTO tasks (id, status) VALUES (?, ?)",
                (task_id, task_status))
    ended = int(_now_iso(ended_offset_min).timestamp())
    con.execute("INSERT INTO task_runs (task_id, outcome, ended_at) VALUES (?, ?, ?)",
                (task_id, outcome, ended))
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


def test_check_kanban_crashes_dedupes_same_task():
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    bd = boards / "jarvis-os"
    bd.mkdir(parents=True, exist_ok=True)
    db = bd / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
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
