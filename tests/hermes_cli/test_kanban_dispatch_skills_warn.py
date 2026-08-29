"""Regression coverage for t_4074165b spec #3 — dispatch-time empty-skills WARN.

A card spawning with no force-loaded skills isn't wrong (many tasks
genuinely need none), so the dispatcher never blocks on it. But it should
log a single visible WARN line per spawn so skill coverage is observable
in worker/dispatcher logs, per Frank's 2026-08-29 directive making skills
on cards standard.
"""
from __future__ import annotations

import logging
import sys
import tempfile

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="kanban_skills_warn_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    return 12345


def test_dispatch_warns_on_empty_skills(isolated_kanban_home, caplog):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="naked task", assignee="default")

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert res.spawned, f"dispatch result: {res!r}"
    assert res.spawned[0][0] == task_id
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        task_id in r.getMessage() and "EMPTY" in r.getMessage()
        for r in warnings
    ), f"expected an empty-skills WARN mentioning {task_id}, got: {[r.getMessage() for r in warnings]}"


def test_dispatch_does_not_warn_when_skills_present(isolated_kanban_home, caplog):
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(
            conn, title="skilled task", assignee="default",
            skills=["skill-hygiene"],
        )

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert res.spawned, f"dispatch result: {res!r}"
    assert res.spawned[0][0] == task_id
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any(
        task_id in r.getMessage() and "EMPTY" in r.getMessage()
        for r in warnings
    ), f"unexpected empty-skills WARN for a task that HAS skills: {[r.getMessage() for r in warnings]}"
