"""Regression: sandbox kanban tests must not inherit live board-routing env.

`/tmp/soak_lean.py` and `/tmp/soak_test_blocked_card_dispatch.py` from the
07-28 blocked-card soak set `HERMES_HOME` and monkeypatched `Path.home`,
but did not clear inherited `HERMES_KANBAN_DB`, `HERMES_KANBAN_BOARD`,
`HERMES_KANBAN_TASK`, or `HERMES_KANBAN_WORKSPACE`. When those scripts ran
inside a kanban worker, the inherited routing env pointed at the real
`sycode-trading` board and scenario cards were written there instead of the
intended sandbox.

These two tests pin the expected behaviour:

1. Tests that intend an isolated kanban DB must clear the board-routing
   env before the first kanban_db call — including ``HERMES_KANBAN_DB``,
   the highest-precedence direct DB-path pin.
2. Inherited worker-like env must NOT modify the real kanban DB's task row
   count, regardless of pinned live board / task / workspace values. The
   test operates on a tmp COPY of the live board so its own failure mode
   cannot contaminate the real DB; the real DB is only opened read-only.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.sandbox_kanban_helper import (
    clear_kanban_routing_env,
    enter_sandbox,
)


LIVE_BOARD_PATH = (
    Path("/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db")
)
LIVE_BOARD_SLUG = "sycode-trading"
WORKER_LIKE_ROUTING = {
    "HERMES_KANBAN_BOARD": LIVE_BOARD_SLUG,
    "HERMES_KANBAN_TASK": "t_sandbox_repro",
    "HERMES_KANBAN_WORKSPACE": "/tmp/sandbox-workspace-fake",
}


def test_enter_sandbox_clears_inherited_board_routing_env(tmp_path, monkeypatch):
    for key, value in WORKER_LIKE_ROUTING.items():
        monkeypatch.setenv(key, value)
    # HERMES_KANBAN_DB is the HIGHEST-precedence routing vector:
    # kanban_db_path() honours the pin unconditionally, before any
    # board/home resolution, and the dispatcher injects it on worker
    # spawn. Pin it to a fake tmp path here and assert it is cleared, so
    # dropping "HERMES_KANBAN_DB" from _WORKER_ROUTING_ENV_KEYS fails this
    # suite. Deliberately a fake path and deliberately only in THIS test:
    # the preflight test below stays pin-free by design — a live-path pin
    # variant would write to the live board, because honouring the pin is
    # intentional kanban_db behaviour, not a regression.
    fake_db_pin = tmp_path / "fake-worker" / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(fake_db_pin))

    clear_kanban_routing_env()
    _, kanban_home = enter_sandbox(tmp_path)

    assert os.environ.get("HERMES_KANBAN_DB") is None
    assert os.environ.get("HERMES_KANBAN_BOARD") is None
    assert os.environ.get("HERMES_KANBAN_TASK") is None
    assert os.environ.get("HERMES_KANBAN_WORKSPACE") is None
    assert os.environ.get("HERMES_KANBAN_HOME") is None
    assert os.environ.get("HERMES_HOME") == str(kanban_home)
    assert Path.home() == tmp_path


def _tasks_count(db_path: Path, *, readonly: bool) -> int:
    """Row count of ``tasks``; ``readonly=True`` opens mode=ro (cannot write)."""
    import sqlite3

    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


def test_sandbox_preflight_blocks_live_board_without_direct_db_pin(
    tmp_path, monkeypatch
):
    if not LIVE_BOARD_PATH.exists():
        pytest.skip("live sycode-trading board not present on this host")

    import sqlite3

    # Detector safety (PR #8 review hardening): a detector for live-board
    # contamination must not contaminate the live board when it fires.
    # Stage a tmp COPY of the live DB inside a fake "live home" laid out
    # exactly like the real one, and point the pre-sandbox HERMES_HOME at
    # it. A regression that leaks the pre-sandbox home into path
    # resolution then writes its junk "repro" row into the copy — never
    # into the real sycode-trading kanban.db, which this test only ever
    # opens read-only (mode=ro). Snapshot via the sqlite backup API so the
    # copy is consistent even while the live board is being written.
    live_home = tmp_path / "live-home"
    live_copy = (
        live_home / ".hermes" / "kanban" / "boards" / LIVE_BOARD_SLUG / "kanban.db"
    )
    live_copy.parent.mkdir(parents=True)
    src = sqlite3.connect(f"file:{LIVE_BOARD_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(str(live_copy))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    clear_kanban_routing_env()
    for key, value in WORKER_LIKE_ROUTING.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", str(live_home / ".hermes"))

    sandbox_root = tmp_path / "sandbox"
    sandbox_root.mkdir()
    _, _ = enter_sandbox(sandbox_root)
    kb.init_db(board=LIVE_BOARD_SLUG)

    real_before = _tasks_count(LIVE_BOARD_PATH, readonly=True)
    copy_before = _tasks_count(live_copy, readonly=False)

    with kb.connect() as conn:
        kb.create_task(conn, title="repro", assignee="test-profile")

    assert _tasks_count(live_copy, readonly=False) == copy_before, (
        "inherited live board-routing env leaked past the sandbox — the "
        "junk row landed in the (copied) live board"
    )
    assert _tasks_count(LIVE_BOARD_PATH, readonly=True) == real_before, (
        "inherited live board-routing env must not write to the live "
        "sycode-trading DB"
    )
