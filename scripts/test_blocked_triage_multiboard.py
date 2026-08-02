#!/usr/bin/env python3
"""Tests for the multi-board generalisation of the blocked-triage pilot.

Covers the jarvis-os extension required by jarvis-os/t_9377b6f0:
- board scope is an explicit allowlist (not a single hard-coded board)
- the PM consumer is derived per board (never the sycode PM on jarvis-os)
- each board carries its own idempotency marker so the proven sycode v1
  comments are neither re-applied nor collided with
- classification comments carry machine-readable BLOCK_KIND / RESUME_GATE /
  AGE_BUCKET fields
- Frank/A3 rows stay HOLD and are never auto-routed
- applying comments never mutates status / assignee / block_kind
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "sycode_blocked_triage_pilot.py"
spec = importlib.util.spec_from_file_location("blocked_triage_pilot", MODULE_PATH)
assert spec is not None and spec.loader is not None
pilot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pilot
spec.loader.exec_module(pilot)

NOW = 1_800_000_000


def make_board(board: str) -> Path:
    root = Path(tempfile.mkdtemp())
    board_dir = root / board
    board_dir.mkdir(parents=True)
    con = sqlite3.connect(board_dir / "kanban.db")
    con.execute(
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
          block_kind TEXT, created_at INTEGER, started_at INTEGER, result TEXT
        )
        """
    )
    con.execute("CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    con.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER, run_id INTEGER)")
    con.execute("CREATE TABLE task_runs (task_id TEXT, outcome TEXT, summary TEXT, error TEXT, started_at INTEGER)")
    con.commit()
    con.close()
    return root


def add_task(
    root: Path,
    board: str,
    task_id: str,
    title: str,
    body: str = "",
    assignee: str = "worker",
    block_kind: str = "needs_input",
    age_h: int = 48,
) -> None:
    con = sqlite3.connect(root / board / "kanban.db")
    ts = NOW - age_h * 3600
    con.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, title, body, assignee, "blocked", block_kind, ts, ts, ""),
    )
    con.commit()
    con.close()


def task_row(root: Path, board: str, task_id: str) -> dict:
    con = sqlite3.connect(root / board / "kanban.db")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    con.close()
    return dict(row)


def comments_for(root: Path, board: str, task_id: str) -> list[str]:
    con = sqlite3.connect(root / board / "kanban.db")
    rows = [str(r[0]) for r in con.execute(
        "SELECT body FROM task_comments WHERE task_id=? ORDER BY rowid", (task_id,)
    ).fetchall()]
    con.close()
    return rows


# --- board scoping -------------------------------------------------------

def test_jarvis_os_is_an_allowed_board():
    root = make_board("jarvis-os")
    db = pilot.board_db(root, "jarvis-os")
    assert db.name == "kanban.db"
    assert db.parent.name == "jarvis-os"


def test_sycode_trading_remains_allowed():
    root = make_board("sycode-trading")
    db = pilot.board_db(root, "sycode-trading")
    assert db.parent.name == "sycode-trading"


def test_unlisted_board_is_still_refused():
    try:
        pilot.board_db(Path(tempfile.mkdtemp()), "upero")
    except ValueError as exc:
        assert "upero" in str(exc)
    else:
        raise AssertionError("expected an unlisted board to be refused")


# --- per-board routing ---------------------------------------------------

def test_pm_consumer_is_board_specific():
    root = make_board("jarvis-os")
    add_task(root, "jarvis-os", "t_pm", "Stalled delegated card", assignee="devops", block_kind="")
    plans, metrics = pilot.build_plans(boards_dir=root, board="jarvis-os", now_epoch=NOW)
    assert plans[0]["recommended_route"] == "pm"
    assert plans[0]["consumer"] == "jarvis-os-pm", plans[0]["consumer"]
    assert metrics["board"] == "jarvis-os"


def test_marker_is_board_specific_so_sycode_v1_comments_do_not_collide():
    assert pilot.marker_for("sycode-trading") == "sycode-blocked-triage-pilot:v1:t_00a73790"
    jarvis_marker = pilot.marker_for("jarvis-os")
    assert jarvis_marker != pilot.marker_for("sycode-trading")
    assert "jarvis-os" in jarvis_marker


# --- classification fields (t_9377b6f0 acceptance #1) --------------------

def test_comment_carries_block_kind_resume_gate_and_age_bucket():
    root = make_board("jarvis-os")
    add_task(root, "jarvis-os", "t_fields", "Some stalled card", block_kind="", age_h=24 * 9)
    plans, _ = pilot.build_plans(boards_dir=root, board="jarvis-os", now_epoch=NOW)
    body = pilot.comment_body(plans[0], board="jarvis-os")
    assert "BLOCK_KIND=" in body
    assert "RESUME_GATE=" in body
    assert "AGE_BUCKET=" in body


def test_age_buckets_are_stable_and_ordered():
    assert pilot.age_bucket(1.0) == "<24h"
    assert pilot.age_bucket(48.0) == "1-7d"
    assert pilot.age_bucket(24 * 9) == "7-30d"
    assert pilot.age_bucket(24 * 40) == ">30d"


def test_untriaged_empty_block_kind_is_reported_as_untriaged():
    root = make_board("jarvis-os")
    add_task(root, "jarvis-os", "t_empty", "Blocked but missing block_kind", block_kind="", age_h=1)
    plans, metrics = pilot.build_plans(
        boards_dir=root, board="jarvis-os", now_epoch=NOW, min_age_hours=24
    )
    assert len(plans) == 1
    assert plans[0]["untriaged_block_kind"] is True
    assert metrics["untriaged_empty_block_kind_plans"] == 1
    assert pilot.comment_body(plans[0], board="jarvis-os").count("BLOCK_KIND=(empty)") == 1


# --- safety gates preserved ---------------------------------------------

def test_frank_gate_rows_stay_hold_on_jarvis_os():
    root = make_board("jarvis-os")
    add_task(
        root, "jarvis-os", "t_a3",
        "FRANK A3: authorize credential rotation", block_kind="capability",
    )
    plans, metrics = pilot.build_plans(boards_dir=root, board="jarvis-os", now_epoch=NOW)
    assert plans[0]["recommended_route"] == "frank_gate"
    assert "HOLD" in plans[0]["recommended_action"]
    assert metrics["frank_gate_auto_routed"] == 0


def test_negated_a3_boundary_does_not_force_frank_gate_on_jarvis_os():
    root = make_board("jarvis-os")
    add_task(
        root, "jarvis-os", "t_safe", "Read-only board hygiene",
        "GATES: A3-safe; no credentials, no live trading, no production deploy.",
        block_kind="",
    )
    plans, _ = pilot.build_plans(boards_dir=root, board="jarvis-os", now_epoch=NOW)
    assert plans[0]["recommended_route"] == "pm"


def test_apply_is_idempotent_and_never_mutates_task_state():
    root = make_board("jarvis-os")
    add_task(root, "jarvis-os", "t_x", "Stalled card", assignee="devops", block_kind="")
    before = task_row(root, "jarvis-os", "t_x")
    plans, _ = pilot.build_plans(boards_dir=root, board="jarvis-os", now_epoch=NOW)
    first = pilot.apply_comments(plans, boards_dir=root, board="jarvis-os", now_epoch=NOW)
    second = pilot.apply_comments(plans, boards_dir=root, board="jarvis-os", now_epoch=NOW + 1)
    after = task_row(root, "jarvis-os", "t_x")
    assert first == {"comment-added": 1}
    assert second == {"already-present": 1}
    assert len(comments_for(root, "jarvis-os", "t_x")) == 1
    for key in ("status", "assignee", "block_kind"):
        assert after[key] == before[key], f"{key} mutated: {before[key]} -> {after[key]}"


def test_cross_board_marker_isolation_does_not_suppress_second_board():
    """A card id present on both boards must still get its own board's comment."""
    root = Path(tempfile.mkdtemp())
    for board in ("sycode-trading", "jarvis-os"):
        (root / board).mkdir(parents=True)
        con = sqlite3.connect(root / board / "kanban.db")
        con.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT,"
            " status TEXT, block_kind TEXT, created_at INTEGER, started_at INTEGER, result TEXT)"
        )
        con.execute("CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
        con.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER, run_id INTEGER)")
        con.commit()
        con.close()
        add_task(root, board, "t_dup", "Same id on both boards", block_kind="")
    for board in ("sycode-trading", "jarvis-os"):
        plans, _ = pilot.build_plans(boards_dir=root, board=board, now_epoch=NOW)
        res = pilot.apply_comments(plans, boards_dir=root, board=board, now_epoch=NOW)
        assert res == {"comment-added": 1}, (board, res)
    assert len(comments_for(root, "jarvis-os", "t_dup")) == 1
    assert len(comments_for(root, "sycode-trading", "t_dup")) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
