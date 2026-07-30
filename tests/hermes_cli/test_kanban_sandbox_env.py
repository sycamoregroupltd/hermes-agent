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
   env before the first kanban_db call.
2. Inherited worker-like env must NOT modify the real kanban DB's task row
   count, regardless of pinned live board / task / workspace values.
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

    clear_kanban_routing_env()
    _, kanban_home = enter_sandbox(tmp_path)

    assert os.environ.get("HERMES_KANBAN_BOARD") is None
    assert os.environ.get("HERMES_KANBAN_TASK") is None
    assert os.environ.get("HERMES_KANBAN_WORKSPACE") is None
    assert os.environ.get("HERMES_KANBAN_HOME") is None
    assert os.environ.get("HERMES_HOME") == str(kanban_home)
    assert Path.home() == tmp_path


def test_sandbox_preflight_blocks_live_board_without_direct_db_pin(
    tmp_path, monkeypatch
):
    if not LIVE_BOARD_PATH.exists():
        pytest.skip("live sycode-trading board not present on this host")

    import sqlite3

    clear_kanban_routing_env()
    for key, value in WORKER_LIKE_ROUTING.items():
        monkeypatch.setenv(key, value)

    _, _ = enter_sandbox(tmp_path)
    kb.init_db(board=LIVE_BOARD_SLUG)

    before = None
    with sqlite3.connect(str(LIVE_BOARD_PATH)) as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    with kb.connect() as conn:
        kb.create_task(conn, title="repro", assignee="test-profile")

    after = None
    with sqlite3.connect(str(LIVE_BOARD_PATH)) as conn:
        after = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    assert before == after, (
        "inherited live board-routing env must not write to the live sycode-trading DB"
    )
