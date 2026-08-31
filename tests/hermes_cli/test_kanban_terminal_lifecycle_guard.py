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
    kb._worker_pid_associations.clear()
    kb._recent_worker_exits.clear()
    yield home
    kb._worker_handles.clear()
    kb._worker_pid_associations.clear()
    kb._recent_worker_exits.clear()


def _claim_with_pid(conn, index: int) -> tuple[str, int, int]:
    task_id = kb.create_task(conn, title=f"lifecycle-{index}", assignee="worker")
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, task_id, claimer=f"{host}:test-{index}")
    assert claimed is not None
    pid = 22000 + index
    kb._set_worker_pid(conn, task_id, pid)
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["current_run_id"]
    assert run_id is not None
    return task_id, pid, int(run_id)


def test_dispatcher_records_pre_reap_predicate_for_ten_clean_exits(kanban_home):
    """Ten-card sample proves clean exits are classified, not silently reaped."""
    conn = kb.connect()
    try:
        tasks = [_claim_with_pid(conn, i) for i in range(10)]
        for _task_id, pid, _run_id in tasks:
            kb._register_worker_handle(pid, SimpleNamespace(poll=lambda: 0))

        result = kb.dispatch_once(
            conn,
            max_spawn=0,
            reconcile_orphans=False,
        )

        assert len(result.terminal_predicate_checks) == 10
        assert {c["task_id"] for c in result.terminal_predicate_checks} == {
            task_id for task_id, _pid, _run_id in tasks
        }
        assert all(c["phase"] == "before_exit_verdict" for c in result.terminal_predicate_checks)
        assert all(c["terminal_signal"] is False for c in result.terminal_predicate_checks)
        assert all(c["enforcement_action"] == "reject_exit_and_run_protocol_violation"
                   for c in result.terminal_predicate_checks)
        assert all(c["association_source"] == "retained_pid_association"
                   for c in result.terminal_predicate_checks)
        assert all(c["predicate"] == "terminal status and current_run_id is NULL"
                   for c in result.terminal_predicate_checks)

        for task_id, _pid, _run_id in tasks:
            checks = [
                event for event in kb.list_events(conn, task_id)
                if event.kind == "terminal_predicate_checked"
            ]
            violations = [
                event for event in kb.list_events(conn, task_id)
                if event.kind == "terminal_lifecycle_violation"
            ]
            assert len(checks) == 1
            assert checks[0].payload["terminal_signal"] is False
            assert len(violations) == 1
            assert kb.get_task(conn, task_id).status == "ready"
    finally:
        conn.close()


def test_pre_reap_predicate_accepts_real_completion_transition(kanban_home):
    """Real completion clears live PID columns but remains positively observed."""
    conn = kb.connect()
    try:
        task_id, pid, run_id = _claim_with_pid(conn, 100)
        kb._register_worker_handle(pid, SimpleNamespace(poll=lambda: 0))

        assert kb.complete_task(
            conn,
            task_id,
            summary="completed through the real terminal transition",
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, task_id).status == "done"
        assert kb.get_task(conn, task_id).worker_pid is None

        assert kb.reap_worker_zombies(conn) == [pid]

        checks = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "terminal_predicate_checked"
        ]
        assert len(checks) == 1
        payload = checks[0].payload
        assert payload["terminal_signal"] is True
        assert payload["enforcement_action"] == "accept_terminal"
        assert payload["association_source"] == "retained_pid_association"
        assert payload["associated_run_id"] == run_id
        assert payload["associated_run_ended_at"] is not None
        assert not [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "terminal_lifecycle_violation"
        ]
        assert kb._worker_handles == {}
        assert kb._worker_pid_associations == {}
    finally:
        conn.close()


def test_pre_reap_predicate_accepts_real_block_transition(kanban_home):
    """Real blocking also clears live PID columns but remains positively observed."""
    conn = kb.connect()
    try:
        task_id, pid, run_id = _claim_with_pid(conn, 101)
        kb._register_worker_handle(pid, SimpleNamespace(poll=lambda: 0))

        assert kb.block_task(
            conn,
            task_id,
            reason="operator review required",
            kind="capability",
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, task_id).status == "blocked"
        assert kb.get_task(conn, task_id).worker_pid is None

        assert kb.reap_worker_zombies(conn) == [pid]

        checks = [
            event for event in kb.list_events(conn, task_id)
            if event.kind == "terminal_predicate_checked"
        ]
        assert len(checks) == 1
        payload = checks[0].payload
        assert payload["terminal_signal"] is True
        assert payload["enforcement_action"] == "accept_terminal"
        assert payload["associated_run_id"] == run_id
        assert payload["associated_run_ended_at"] is not None
        assert kb._worker_handles == {}
        assert kb._worker_pid_associations == {}
    finally:
        conn.close()
