"""Regression tests for the kanban worker in-process crash handler.

Covers the mechanism-gap card t_498c8b86: a dispatcher-owned worker that exits
without a terminal kanban signal (crash, signal, or clean rc=0 no-terminal)
must leave its card blocked with a traceable comment instead of stale
running/ready residue.

Bounds exercised:
- Blocks only when no terminal tool succeeded.
- Never blocks on the provider-quota EX_TEMPFAIL sentinel (dispatcher retry).
- Idempotent (one block, one comment).
- Emits a block from the signal hook (the os._exit path skips atexit).
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    import hermes_cli.kanban_db as kb

    kb.init_db()
    return home


@pytest.fixture
def crash_state(monkeypatch):
    """Reset crash-handler module state and pin a worker task in the env."""
    import agent.kanban_crash_handler as kh
    import hermes_cli.kanban_db as kb

    # Reset module-level state so tests are order-independent.
    kh._terminal_sent = False
    kh._block_emitted = False
    kh._exit_code = None
    kh._last_error = None
    kh._atexit_registered = False

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="crash-handler test", assignee="worker")
        kb.claim_task(conn, tid)
        run = kb.latest_run(conn, tid)
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.id))
    monkeypatch.delenv("HERMES_KANBAN_GOAL_MODE", raising=False)
    return {"kh": kh, "kb": kb, "tid": tid, "run_id": run.id}


def _comments(kb, conn, tid):
    return kb.list_comments(conn, tid)


def test_atexit_blocks_on_no_terminal_exit(kanban_home, crash_state):
    kh, kb, tid, run_id = (
        crash_state["kh"], crash_state["kb"], crash_state["tid"], crash_state["run_id"],
    )
    kh.install()
    # Simulate a worker that returns rc=0 without ever calling a terminal tool.
    kh.record_worker_exit(0, "provider 401 unauthorized")
    kh.atexit_crash_block()

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "no-terminal worker exit must block the card"
        assert task.block_kind == "capability"
        bodies = [c.body for c in _comments(kb, conn, tid)]
        assert any("worker_crash_or_protocol_violation" in b for b in bodies), (
            "traceability comment must carry the crash reason"
        )
        assert any("provider 401 unauthorized" in b for b in bodies), (
            "traceability comment must carry last_error"
        )
    finally:
        conn.close()


def test_atexit_stands_down_after_terminal(kanban_home, crash_state):
    kh, kb, tid, run_id = (
        crash_state["kh"], crash_state["kb"], crash_state["tid"], crash_state["run_id"],
    )
    kh.install()
    # Worker legitimately completed → no block, no comment.
    kh.mark_terminal_sent()
    kh.record_worker_exit(0)
    kh.atexit_crash_block()

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running", (
            "graceful terminal exit must NOT be blocked"
        )
        assert _comments(kb, conn, tid) == []
    finally:
        conn.close()


def test_atexit_skips_rate_limit_sentinel(kanban_home, crash_state):
    kh, kb, tid, run_id = (
        crash_state["kh"], crash_state["kb"], crash_state["tid"], crash_state["run_id"],
    )
    kh.install()
    # Provider-quota exit (EX_TEMPFAIL sentinel) is dispatcher-owned retry.
    kh.record_worker_exit(kb.KANBAN_RATE_LIMIT_EXIT_CODE, "quota exhausted")
    kh.atexit_crash_block()

    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "running", (
            "rate-limit sentinel must NOT block — dispatcher owns the retry"
        )
        assert _comments(kb, conn, tid) == []
    finally:
        conn.close()


def test_signal_hook_emits_block(kanban_home, crash_state):
    kh, kb, tid, run_id = (
        crash_state["kh"], crash_state["kb"], crash_state["tid"], crash_state["run_id"],
    )
    # No install() needed for the signal hook — it is invoked directly by the
    # CLI signal handler before the os._exit path.
    kh.emit_block_on_signal(signal.SIGTERM)

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        bodies = [c.body for c in _comments(kb, conn, tid)]
        assert any("SIGTERM" in b for b in bodies)
    finally:
        conn.close()


def test_block_is_idempotent(kanban_home, crash_state):
    kh, kb, tid, run_id = (
        crash_state["kh"], crash_state["kb"], crash_state["tid"], crash_state["run_id"],
    )
    kh.install()
    kh.atexit_crash_block()
    kh.atexit_crash_block()  # second call must be a no-op
    kh.emit_block_on_signal(signal.SIGINT)  # and a late signal must not double-block

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        # Exactly one comment (one traceable block).
        assert len(_comments(kb, conn, tid)) == 1
    finally:
        conn.close()


def test_mark_terminal_sent_wired_into_kanban_complete(kanban_home, crash_state):
    """A real kanban_complete success must stand the crash handler down."""
    kh, kb, tid, run_id = (
        crash_state["kh"], crash_state["kb"], crash_state["tid"], crash_state["run_id"],
    )
    import tools.kanban_tools as ktools

    out = ktools._handle_complete({"task_id": tid, "summary": "done"})
    assert "error" not in out.lower(), out
    assert kh._terminal_sent is True, (
        "kanban_complete success must flip the terminal-sent flag"
    )
    # And the crash handler must then stand down.
    kh.record_worker_exit(0)
    kh.atexit_crash_block()
    conn = kb.connect()
    try:
        assert kb.get_task(conn, tid).status == "done"
    finally:
        conn.close()


def test_real_atexit_shutdown_blocks(kanban_home, crash_state):
    """Regression: the atexit hook must block during REAL interpreter shutdown.

    The prior implementation (PR #27) failed here: its atexit hook lazily
    imported ``hermes_cli.kanban_db`` while the interpreter was tearing down,
    which raised ``RuntimeError: can't register atexit after shutdown`` and was
    swallowed — leaving the card stranded in ``running``. This test spawns a
    subprocess that arms the handler and dies on an unhandled exception, the
    real crash path, and asserts the card lands blocked. ``install()``'s eager
    connect preloads the import chain so the shutdown path reuses cached modules.
    """
    import subprocess, textwrap

    kh, kb, tid, run_id = (
        crash_state["kh"], crash_state["kb"], crash_state["tid"], crash_state["run_id"],
    )
    db_path = os.path.join(kanban_home, "kanban", "default", "kanban.db")
    # Locate the real DB path the isolated board resolved to.
    conn = kb.connect()
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()

    script = textwrap.dedent(
        """
        import os, sys
        from agent.kanban_crash_handler import install
        install()
        raise RuntimeError("simulated unhandled crash before terminal signal")
        """
    )
    sub_env = dict(os.environ)
    sub_env.update(
        {
            "HERMES_HOME": kanban_home,
            "HERMES_KANBAN_DB": db_path,
            "HERMES_KANBAN_TASK": tid,
            "HERMES_KANBAN_RUN_ID": str(run_id),
            "PYTHONPATH": os.path.join(os.path.dirname(os.path.dirname(__file__)), ".."),
        }
    )
    for k in ["HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT"]:
        sub_env.pop(k, None)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=sub_env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    # The unhandled exception propagates as rc=1; the handler must still block.
    assert proc.returncode != 0

    conn = kb.connect()
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            "real atexit shutdown must block the card (shutdown-import regression)"
        )
        bodies = [c.body for c in _comments(kb, conn, tid)]
        assert any("worker_crash_or_protocol_violation" in b for b in bodies)
    finally:
        conn.close()
