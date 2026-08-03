#!/usr/bin/env python3
"""Tests for scripts/board_pm_triage_visibility.py.

Covers the three ordered guards (active queue, open PM-triage card, recent PM
activity), the dry-run contract (prints intent, never writes), the create path
(only when all guards pass; only its OWN idempotency-keyed card), and the hard
non-mutation rule: existing tasks — including the narrow PM-owned cards named in
the spec (t_cfbbb102, t_af99cb12) — are never modified.

Run:  python3 scripts/test_board_pm_triage_visibility.py
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "board_pm_triage_visibility.py"
spec = importlib.util.spec_from_file_location("board_pm_triage_visibility", MODULE_PATH)
assert spec is not None and spec.loader is not None
vis = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = vis
spec.loader.exec_module(vis)

NOW = 1_800_000_000

TASKS_SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
  block_kind TEXT, created_at INTEGER, started_at INTEGER, result TEXT
)
"""


def make_root() -> Path:
    root = Path(tempfile.mkdtemp())
    board_dir = root / "sycode-trading"
    board_dir.mkdir(parents=True)
    con = sqlite3.connect(board_dir / "kanban.db")
    con.execute(TASKS_SCHEMA)
    con.execute("CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    con.commit()
    con.close()
    # Point the module at the temp board tree so tests never touch the live board.
    vis.BOARDS = root
    return root


def add_task(
    root: Path,
    task_id: str,
    title: str,
    assignee: str = "worker",
    status: str = "ready",
    body: str = "",
    age_h: int = 48,
) -> None:
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    ts = NOW - age_h * 3600
    con.execute(
        "INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
        (task_id, title, body, assignee, status, "", ts, ts, ""),
    )
    con.commit()
    con.close()


def add_comment(root: Path, task_id: str, author: str, age_h: float) -> None:
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    con.execute(
        "INSERT INTO task_comments VALUES (?,?,?,?)",
        (task_id, author, "comment", int(NOW - age_h * 3600)),
    )
    con.commit()
    con.close()


def task_rows(root: Path) -> list[tuple]:
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    con.row_factory = sqlite3.Row
    rows = [tuple(r) for r in con.execute("SELECT * FROM tasks ORDER BY id")]
    con.close()
    return rows


def count_tasks(root: Path) -> int:
    con = sqlite3.connect(root / "sycode-trading" / "kanban.db")
    n = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    con.close()
    return int(n)


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeSubprocess:
    """Stub subprocess module: records commands, never executes anything."""

    def __init__(self, payload: str = '{"id":"t_new","title":"x"}'):
        self.payload = payload
        self.commands: list[list[str]] = []

    def run(self, cmd, **kwargs):  # noqa: ANN001
        self.commands.append(list(cmd))
        return FakeCompletedProcess(self.payload)


def patch_subprocess(payload: str = '{"id":"t_new","title":"x"}') -> FakeSubprocess:
    fake = FakeSubprocess(payload)
    vis.subprocess = fake  # type: ignore[attr-defined]
    return fake


def test_connect_missing_board_raises():
    root = Path(tempfile.mkdtemp())
    vis.BOARDS = root
    try:
        vis.connect_board("nope")
    except FileNotFoundError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError for missing board")
    # left-over board dir must NOT be created by connect
    assert not (root / "nope").exists()


def test_silent_when_no_active_tasks():
    root = make_root()
    add_task(root, "t_done", "Finished", status="done")
    add_task(root, "t_arch", "Archived", status="archived")
    out = vis.run("sycode-trading", "sycode-trading-pm", dry_run=False, source="test", recent_pm_hours=6.0)
    assert out == ""


def test_silent_when_open_pm_triage_card():
    root = make_root()
    add_task(root, "t_ready", "Active work", status="ready")
    add_task(
        root,
        "t_vis",
        "PM TRIAGE VISIBILITY: sycode-trading active queue review 2026-08-03",
        assignee="sycode-trading-pm",
        status="todo",
    )
    out = vis.run("sycode-trading", "sycode-trading-pm", dry_run=False, source="test", recent_pm_hours=6.0)
    assert out == ""


def test_silent_when_recent_pm_activity():
    root = make_root()
    add_task(root, "t_ready", "Active work", status="ready")
    add_comment(root, "t_ready", "sycode-trading-pm", age_h=1.0)
    out = vis.run("sycode-trading", "sycode-trading-pm", dry_run=False, source="test", recent_pm_hours=6.0)
    assert out == ""


def test_dry_run_prints_intent_without_writing():
    root = make_root()
    add_task(root, "t_ready", "Active work", status="ready")
    before = count_tasks(root)
    out = vis.run("sycode-trading", "sycode-trading-pm", dry_run=True, source="test", recent_pm_hours=6.0)
    assert out.startswith("DRY_RUN")
    assert "sycode-trading" in out
    assert "pm-triage-visibility:sycode-trading:" in out
    assert count_tasks(root) == before, "dry-run must not write to the board"


def test_create_path_only_when_guards_pass_and_only_own_card():
    root = make_root()
    add_task(root, "t_ready", "Active work", status="ready")
    before = task_rows(root)
    fake = patch_subprocess()
    out = vis.run("sycode-trading", "sycode-trading-pm", dry_run=False, source="test", recent_pm_hours=6.0)
    assert out.startswith("CREATED_OR_EXISTING")
    assert fake.commands, "create path must invoke the kanban CLI"
    cmd = fake.commands[0]
    joined = " ".join(cmd)
    assert "--board" in joined and "sycode-trading" in joined
    assert "pm-triage-visibility:sycode-trading:" in joined
    assert "--assignee" in joined and "sycode-trading-pm" in joined
    assert "--priority" in joined and "75" in joined
    # The board itself is untouched: creation happens via the mocked CLI only.
    assert task_rows(root) == before


def test_run_never_modifies_existing_narrow_pm_cards():
    # Spec-named narrow PM cards (owned by jarvis) must never be modified.
    root = make_root()
    add_task(root, "t_cfbbb102", "Relabel fusion report sidecar row", assignee="jarvis", status="archived")
    add_task(root, "t_af99cb12", "CLEAN-EPOCH DAY-14 GRADUATION", assignee="jarvis", status="done")
    add_task(root, "t_ready", "Active work", status="ready")
    before = task_rows(root)
    fake = patch_subprocess()
    vis.run("sycode-trading", "sycode-trading-pm", dry_run=False, source="test", recent_pm_hours=6.0)
    assert task_rows(root) == before, "existing cards (incl. narrow PM cards) must be byte-identical"
    assert fake.commands, "create path should have fired when guards pass"


def test_board_isolation_two_boards():
    root = make_root()
    other = root / "other-board"
    other.mkdir(parents=True)
    con = sqlite3.connect(other / "kanban.db")
    con.execute(TASKS_SCHEMA)
    con.execute("CREATE TABLE task_comments (task_id TEXT, author TEXT, body TEXT, created_at INTEGER)")
    con.commit()
    con.close()
    add_task(root, "t_ready", "Active on sycode-trading", status="ready")
    other_con = sqlite3.connect(other / "kanban.db")
    other_con.execute("INSERT INTO tasks VALUES ('t_other','Other','','w','ready','','1','1','')")
    other_con.commit()
    other_con.close()

    fake = patch_subprocess()
    out = vis.run("sycode-trading", "sycode-trading-pm", dry_run=False, source="test", recent_pm_hours=6.0)
    assert out.startswith("CREATED_OR_EXISTING")
    assert all("other-board" not in " ".join(c) for c in fake.commands)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
