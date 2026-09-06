"""Fail-closed restart accounting for embedded Kanban workers."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin
from hermes_cli import kanban_db as kb
from tests.gateway.restart_test_helpers import make_restart_runner


class _Watcher(GatewayKanbanWatchersMixin):
    pass


def test_running_worker_scan_fails_closed_on_board_enumeration_error(monkeypatch):
    def fail_enumeration(*, include_archived=False):
        raise OSError("boards root unavailable")

    monkeypatch.setattr(kb, "list_boards", fail_enumeration)

    with pytest.raises(kb.KanbanWorkerScanError, match="board enumeration failed"):
        _Watcher()._kanban_running_workers()


def test_running_worker_scan_fails_closed_on_per_board_db_error(monkeypatch):
    monkeypatch.setattr(kb, "list_boards", lambda **_: [{"slug": "jarvis-os"}])

    def fail_connect(*, board):
        raise OSError(f"cannot open {board}")

    monkeypatch.setattr(kb, "connect", fail_connect)

    with pytest.raises(kb.KanbanWorkerScanError, match="board jarvis-os"):
        _Watcher()._kanban_running_workers()


def test_restart_interrupt_does_not_return_zero_on_scan_failure(monkeypatch):
    monkeypatch.setattr(kb, "list_boards", lambda **_: [{"slug": "jarvis-os"}])
    monkeypatch.setattr(
        kb,
        "connect",
        lambda **_: (_ for _ in ()).throw(OSError("db unavailable")),
    )

    with pytest.raises(kb.KanbanWorkerScanError):
        _Watcher()._interrupt_kanban_workers_for_restart()


def test_active_work_count_propagates_kanban_scan_error():
    class Runner:
        _running_agents = {}

        @staticmethod
        def _active_cron_job_count():
            return 0

        @staticmethod
        def _active_api_run_count():
            return 0

        @staticmethod
        def _active_deferred_agent_worker_count():
            return 0

        @staticmethod
        def _active_kanban_worker_count():
            raise kb.KanbanWorkerScanError("board scan failed")

    with pytest.raises(kb.KanbanWorkerScanError):
        # Call the production implementation against the minimal test double;
        # this asserts the gateway cannot convert an unknown worker set to 0.
        from gateway.run import GatewayRunner

        GatewayRunner._active_work_count(Runner())


@pytest.mark.asyncio
async def test_restart_stays_draining_when_kanban_scan_fails():
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()

    def fail_worker_count():
        raise kb.KanbanWorkerScanError("board scan failed")

    runner._active_kanban_worker_count = fail_worker_count

    assert runner.request_restart(detached=False, via_service=True) is True
    await runner._restart_task

    runner.stop.assert_not_awaited()
    assert runner._draining is True
    assert runner._restart_task_started is False
