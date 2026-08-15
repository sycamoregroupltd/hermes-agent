"""Tests for the kanban worker crash handler (t_498c8b86 mechanism gap).

A dispatcher-spawned worker must end with a terminal board signal
(``kanban_complete`` / ``kanban_block`` / ``kanban_request_review`` /
``kanban_request_changes``). When it instead exits non-gracefully without one,
the task should reach a terminal ``blocked`` state with evidence rather than
lingering as running/ready residue. This module tests the in-process crash
handler (catchable exits) and the dispatcher's auto-block-with-comment for the
uncatchable (SIGKILL / os._exit) path.
"""

from __future__ import annotations

import json
import os

import pytest

from agent import kanban_crash_handler as kch


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Isolated worker environment with a claimed running task."""
    from pathlib import Path as _Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="crash-test", assignee="test-worker")
        kb.claim_task(conn, tid)
        kb._set_worker_pid(conn, tid, os.getpid())
        run = kb.latest_run(conn, tid)
        run_id = run.id if run else None
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    if run_id:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "testhost:mock")
    return {"tid": tid, "run_id": run_id}


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """Reset the module's in-memory flags between tests."""
    kch._armed = False
    kch._rate_limit_exit = False
    yield
    kch._armed = False
    kch._rate_limit_exit = False


# ---------------------------------------------------------------------------
# Enabling / arming
# ---------------------------------------------------------------------------


def test_disabled_without_worker_env(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CRASH_HANDLER", raising=False)
    assert kch.kanban_crash_handler_enabled() is False


def test_enabled_with_worker_env(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    monkeypatch.delenv("HERMES_KANBAN_CRASH_HANDLER", raising=False)
    assert kch.kanban_crash_handler_enabled() is True


def test_can_disable_via_env(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_HANDLER", "0")
    assert kch.kanban_crash_handler_enabled() is False
    kch.arm_kanban_crash_handler()
    assert kch._armed is False


# ---------------------------------------------------------------------------
# Catchable exit: SIGTERM-style / explicit emit
# ---------------------------------------------------------------------------


def test_emit_crash_block_blocks_running_task(worker_env):
    """A non-graceful exit without a terminal signal blocks the running task."""
    tid = worker_env["tid"]
    assert kch.emit_kanban_crash_block(note="test SIGTERM crash") is True

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        comments = kb.list_comments(conn, tid)
        assert len(comments) >= 1
        assert "worker_crash_or_protocol_violation" in comments[-1].body
        assert "test SIGTERM crash" in comments[-1].body
        # The run was ended as blocked.
        runs = kb.list_runs(conn, tid)
        assert any(r.outcome == "blocked" for r in runs)
    finally:
        conn.close()


def test_emit_crash_block_noop_when_already_terminal(worker_env):
    """Idempotent: does not clobber a task that already reached a terminal state."""
    tid = worker_env["tid"]
    # Simulate a legitimate block first.
    assert kch.emit_kanban_crash_block(note="first crash") is True

    # Second emit is a no-op (task no longer running/ready).
    assert kch.emit_kanban_crash_block(note="second crash") is False

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        comments = kb.list_comments(conn, tid)
        # Only the first block produced a comment; the second was a no-op.
        assert len(comments) == 1
    finally:
        conn.close()


def test_emit_crash_block_skips_rate_limit(worker_env):
    """A rate-limit sentinel exit must NOT be converted into a block."""
    tid = worker_env["tid"]
    kch.mark_rate_limit_exit()
    assert kch.emit_kanban_crash_block(note="rate limited") is False

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        # Task left untouched (still running) — the dispatcher requeues it.
        assert task.status == "running"
    finally:
        conn.close()


def test_emit_crash_block_noop_without_worker_env(monkeypatch, worker_env):
    """Without a worker task env, emit is a safe no-op."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kch.emit_kanban_crash_block(note="no task") is False


# ---------------------------------------------------------------------------
# atexit semantics: only genuine crashes, not clean rc=0
# ---------------------------------------------------------------------------


def test_atexit_fires_on_unhandled_exception(worker_env, monkeypatch):
    """An unhandled exception at exit triggers the crash block."""
    tid = worker_env["tid"]
    called = {}
    real = kch._emit_crash_block

    def fake(note=None):
        called["n"] = note
        return real(note=note)

    monkeypatch.setattr(kch, "_emit_crash_block", fake)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        kch._on_atexit()

    # Emit was invoked (note may be None → the handler synthesises its own).
    assert "n" in called
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "blocked"
    finally:
        conn.close()


def test_atexit_skips_clean_systemexit(worker_env, monkeypatch):
    """A clean SystemExit(0) must NOT trigger the crash block (bounded retry)."""
    called = {}
    monkeypatch.setattr(kch, "_emit_crash_block", lambda note=None: called.__setitem__("n", note) or True)
    try:
        raise SystemExit(0)
    except SystemExit:
        kch._on_atexit()
    assert called == {}


def test_atexit_skips_no_exception(worker_env, monkeypatch):
    """A clean interpreter shutdown (no exception) must NOT trigger the block."""
    called = {}
    monkeypatch.setattr(kch, "_emit_crash_block", lambda note=None: called.__setitem__("n", note) or True)
    kch._on_atexit()
    assert called == {}


# ---------------------------------------------------------------------------
# Dispatcher auto-block-with-comment for uncatchable SIGKILL / os._exit
# ---------------------------------------------------------------------------


def test_dispatcher_autoblock_emits_traceable_comment(worker_env, monkeypatch):
    """detect_crashed_workers auto-block (uncatchable crash) leaves a comment.

    SIGKILL / os._exit bypass in-process atexit and signal handlers, so those
    crashed workers are reclaimed by the dispatcher's detect_crashed_workers.
    When the failure limit trips, the breaker auto-blocks the task AND (with
    this mechanism) stamps a traceable ``[auto-block]`` comment on the card.
    """
    import hermes_cli.kanban_db as _kb
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    conn = _kb.connect()
    try:
        tid = _kb.create_task(conn, title="uncatchable-crash", assignee="factory")
        host_prefix = _kb._claimer_id().split(":", 1)[0]

        def _drive_sigkill(fake_pid):
            claimed = _kb.claim_task(conn, tid, claimer=f"{host_prefix}:mock")
            assert claimed is not None, "task was not claimable for the next attempt"
            _kb._set_worker_pid(conn, tid, fake_pid)
            _kb._record_worker_exit(fake_pid, 0x0009)  # WIFSIGNALED, signal 9
            original_alive = _kb._pid_alive
            _kb._pid_alive = lambda p: False
            try:
                return _kb.detect_crashed_workers(conn)
            finally:
                _kb._pid_alive = original_alive

        # Two consecutive SIGKILL crashes trip the unified breaker (limit 2).
        _drive_sigkill(1001)
        _drive_sigkill(1002)
    finally:
        conn.close()

    conn = _kb.connect()
    try:
        task = _kb.get_task(conn, tid)
        assert task.status == "blocked"
        comments = _kb.list_comments(conn, tid)
        assert len(comments) >= 1
        assert "[auto-block]" in comments[-1].body
        assert "outcome=crashed" in comments[-1].body
    finally:
        conn.close()
