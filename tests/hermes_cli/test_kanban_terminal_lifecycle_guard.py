"""Dispatcher-owned terminal lifecycle guard regressions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    kb._worker_handles.clear()
    kb._recent_worker_exits.clear()
    yield home
    kb._worker_handles.clear()
    kb._recent_worker_exits.clear()


def _claim_with_pid(conn, index: int) -> tuple[str, int]:
    task_id = kb.create_task(conn, title=f"lifecycle-{index}", assignee="worker")
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, task_id, claimer=f"{host}:test-{index}")
    assert claimed is not None
    pid = 22000 + index
    kb._set_worker_pid(conn, task_id, pid)
    return task_id, pid


def test_dispatcher_records_pre_reap_predicate_for_ten_clean_exits(kanban_home):
    """Ten-card sample proves clean exits are classified, not silently reaped."""
    conn = kb.connect()
    try:
        tasks = [_claim_with_pid(conn, i) for i in range(10)]
        for _task_id, pid in tasks:
            kb._register_worker_handle(pid, SimpleNamespace(poll=lambda: 0))

        result = kb.dispatch_once(
            conn,
            max_spawn=0,
            reconcile_orphans=False,
        )

        assert len(result.terminal_predicate_checks) == 10
        assert {c["task_id"] for c in result.terminal_predicate_checks} == {
            task_id for task_id, _pid in tasks
        }
        assert all(c["phase"] == "before_exit_verdict" for c in result.terminal_predicate_checks)
        assert all(c["terminal_signal"] is False for c in result.terminal_predicate_checks)
        assert all(c["predicate"] == "terminal status and current_run_id is NULL"
                   for c in result.terminal_predicate_checks)

        for task_id, _pid in tasks:
            checks = [
                event for event in kb.list_events(conn, task_id)
                if event.kind == "terminal_predicate_checked"
            ]
            assert len(checks) == 1
            assert checks[0].payload["terminal_signal"] is False
            assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()


def test_pre_reap_predicate_accepts_durable_terminal_transition(kanban_home):
    """A task already terminal is observed as valid and is not reclassified."""
    conn = kb.connect()
    try:
        task_id, pid = _claim_with_pid(conn, 100)
        conn.execute(
            "UPDATE tasks SET status='done', current_run_id=NULL WHERE id=?",
            (task_id,),
        )
        conn.commit()
        kb._register_worker_handle(pid, SimpleNamespace(poll=lambda: 0))

        reaped = kb.reap_worker_zombies(conn)

        assert reaped == [pid]
        checks = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "terminal_predicate_checked"
        ]
        assert len(checks) == 1
        assert checks[0].payload["terminal_signal"] is True
        assert checks[0].payload["status"] == "done"
        assert kb._worker_handles == {}
    finally:
        conn.close()
