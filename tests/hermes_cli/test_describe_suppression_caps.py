"""Regression tests: describe_suppression reports cap + per-profile deferrals."""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_describe_suppression_test_")
    for prof in ("alpha", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch
    monkeypatch.setattr(kanban_db_dispatch, "_system_memory_sample", dict)
    yield kanban_db


def _fake_spawn(*args, **kwargs):
    return 12345


def test_describe_suppression_reports_per_profile_cap(isolated_kanban_home):
    kb = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"a{i}", assignee="alpha")
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False,
                                max_in_progress_per_profile=1)
    assert len(res.skipped_per_profile_capped) == 2
    text = kbd.describe_suppression([res])
    assert "per_profile_cap=2" in text


def test_describe_suppression_reports_board_cap(isolated_kanban_home):
    kb = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="a0", assignee="alpha")
    # Pretend one worker already running on this board by setting max_spawn=0
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True,
                                max_spawn=0)
    text = kbd.describe_suppression([res])
    assert "board_cap=1" in text


def test_describe_suppression_reports_global_cap(isolated_kanban_home):
    kb = isolated_kanban_home
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="a0", assignee="alpha")
    with kbc.connect_closing() as conn:
        res = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True,
                                max_in_progress=0)
    text = kbd.describe_suppression([res])
    assert "global_cap=1" in text


def test_describe_suppression_empty_when_no_caps():
    from hermes_cli import kanban_db_dispatch as kbd
    res = kbd.DispatchResult()
    assert kbd.describe_suppression([res]) == ""
