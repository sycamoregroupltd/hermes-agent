#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "sycode_blocked_triage_pilot.py"
spec = importlib.util.spec_from_file_location("sycode_blocked_triage_pilot", MODULE_PATH)
assert spec is not None and spec.loader is not None
pilot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pilot
spec.loader.exec_module(pilot)

NOW = 1_800_000_000


def make_board() -> Path:
    root = Path(tempfile.mkdtemp())
    board_dir = root / "sycode-trading"
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


def add_task(root: Path, task_id: str, title: str, body: str = "", assignee: str = "worker", block_kind: str = "needs_input", age_h: int = 48) -> None:
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    ts = NOW - age_h * 3600
    con.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, title, body, assignee, "blocked", block_kind, ts, ts, ""),
    )
    con.commit()
    con.close()


def add_comment(root: Path, task_id: str, body: str) -> None:
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    con.execute("INSERT INTO task_comments VALUES (?,?,?,?)", (task_id, "reviewer", body, NOW - 10))
    con.commit()
    con.close()


def task_row(root: Path, task_id: str):
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    con.close()
    return dict(row)


def comment_count(root: Path, task_id: str) -> int:
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    n = con.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (task_id,)).fetchone()[0]
    con.close()
    return int(n)


def test_refuses_board_outside_the_allowlist():
    """Scope widened to an allowlist (t_6240a616): jarvis-os is now permitted,
    but any board outside the allowlist is still refused."""
    try:
        pilot.board_db(Path(tempfile.mkdtemp()), "upero")
    except ValueError as exc:
        assert "upero" in str(exc)
    else:
        raise AssertionError("expected a board outside the allowlist to be refused")


def test_allowlisted_boards_are_accepted():
    root = Path(tempfile.mkdtemp())
    for board in ("sycode-trading", "jarvis-os"):
        assert pilot.board_db(root, board).parent.name == board


def test_routes_capability_and_a3_to_frank_gate_hold():
    root = make_board()
    add_task(root, "t_a3", "FRANK/A3 GATE: approve credential rotation", block_kind="capability")
    plans, metrics = pilot.build_plans(boards_dir=root, now_epoch=NOW)
    assert len(plans) == 1
    assert plans[0]["recommended_route"] == "frank_gate"
    assert "HOLD" in plans[0]["recommended_action"]
    assert metrics["frank_gate_auto_routed"] == 0


def test_negated_a3_boundary_does_not_force_frank_gate():
    root = make_board()
    add_task(
        root,
        "t_safe",
        "Read-only board hygiene",
        "GATES: A3-safe; no credentials, no live trading, no production deploy. Needs PM routing.",
        block_kind="needs_input",
    )
    plans, _metrics = pilot.build_plans(boards_dir=root, now_epoch=NOW)
    assert plans[0]["recommended_route"] == "pm"


def test_review_verdict_routes_to_reviewer():
    root = make_board()
    add_task(root, "t_review", "Review-required handoff follow-up")
    add_comment(root, "t_review", "REVIEW_VERDICT=APPROVED on source task; unblock or close as appropriate")
    plans, _metrics = pilot.build_plans(boards_dir=root, now_epoch=NOW)
    assert plans[0]["recommended_route"] == "reviewer"


def test_empty_block_kind_younger_than_age_is_still_eligible_pm():
    root = make_board()
    add_task(root, "t_empty", "Blocked but missing block_kind", block_kind="", age_h=1)
    plans, metrics = pilot.build_plans(boards_dir=root, now_epoch=NOW, min_age_hours=24)
    assert len(plans) == 1
    assert plans[0]["recommended_route"] == "pm"
    assert metrics["untriaged_empty_block_kind_plans"] == 1


def test_apply_comments_is_idempotent_and_does_not_mutate_task_state():
    root = make_board()
    add_task(root, "t_pm", "Stalled delegated card", assignee="platform-builder", block_kind="needs_input")
    before = task_row(root, "t_pm")
    plans, _metrics = pilot.build_plans(boards_dir=root, now_epoch=NOW)
    result1 = pilot.apply_comments(plans, boards_dir=root, now_epoch=NOW)
    result2 = pilot.apply_comments(plans, boards_dir=root, now_epoch=NOW + 1)
    after = task_row(root, "t_pm")
    assert result1 == {"comment-added": 1}
    assert result2 == {"already-present": 1}
    assert comment_count(root, "t_pm") == 1
    for key in ("status", "assignee", "block_kind"):
        assert after[key] == before[key], f"{key} mutated: {before[key]} -> {after[key]}"


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
