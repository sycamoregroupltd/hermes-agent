"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture
def kanban_board(tmp_path: Path, monkeypatch, clear_kanban_env):
    """An isolated, empty board under a temp HERMES_HOME.

    Every ``HERMES_KANBAN_*`` pin is cleared, not just the task id: the suite can run
    inside a real dispatcher-spawned worker, where an inherited ``HERMES_KANBAN_DB``
    would make the guard read (and this fixture write) the live board.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _review_handoff(conn, tid: str) -> tuple[str, int]:
    """Drive a card through implementer → review → reviewer claim.

    Returns ``(task_id, reviewer_run_id)``; the implementer's run is already closed by
    ``request_review``, so the reviewer run is the only live one.
    """
    kb.claim_task(conn, tid)
    implementer_run = kb.get_task(conn, tid).current_run_id
    assert kb.request_review(
        conn, tid, summary="implemented", expected_run_id=implementer_run,
    ) is True
    claimed = kb.claim_review_task(conn, tid)
    assert claimed is not None and claimed.current_run_id is not None
    return tid, int(claimed.current_run_id)


def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


# ── Board-truth guard ────────────────────────────────────────────────
# The nudge's liveness source is the board, not the message history: a refused
# terminal call and a successful one look identical in the transcript. These pin
# both directions — suppressed once the run is closed by ANY path, still emitted
# while the run is genuinely open.


def test_no_nudge_after_review_handoff_closed_the_run(
    kanban_board: Path, clear_kanban_env,
) -> None:
    """Regression: a reviewer that handed the card back must not be nudged to
    re-terminate. Under the message-history-only check the board had already
    advanced to ``ready`` (run closed, ``ended_at`` set) yet the worker was told
    "task is still running" — the only reachable answers then were a false
    ``kanban_complete`` (approving a card sent back for changes) or a
    ``kanban_block`` the board refuses."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review lane", assignee="implementer")
        _, reviewer_run = _review_handoff(conn, tid)
        clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
        clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(reviewer_run))
        ok, implementer = kb.request_changes(
            conn, tid, reason="please fix X", expected_run_id=reviewer_run,
        )
        assert ok is True and implementer == "implementer"
        # The card left review and the reviewer's run is closed.
        assert kb.get_task(conn, tid).status == "ready"
        ended = conn.execute(
            "SELECT ended_at FROM task_runs WHERE id = ?", (reviewer_run,),
        ).fetchone()
        assert ended["ended_at"] is not None

    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_request_changes", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "kanban_request_changes",
            "tool_call_id": "1",
            "content": '{"ok": true, "status": "ready"}',
        },
    ]
    # Not a recognized terminal tool — the transcript alone cannot tell success
    # from refusal, which is exactly why the board must be consulted.
    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


def test_no_nudge_when_the_task_left_running_without_a_run_id(
    kanban_board: Path, clear_kanban_env,
) -> None:
    """A worker with no dispatcher run id still reads the task row: a task that is
    no longer ``running`` owes no terminal call."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="done already", assignee="worker")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.complete_task(conn, tid, summary="finished", expected_run_id=run_id)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    assert build_kanban_stop_nudge(messages=[], attempts=0) is None


def test_nudge_still_fires_while_the_run_is_open(
    kanban_board: Path, clear_kanban_env,
) -> None:
    """The guard's protection is intact: a live run that stops without a terminal
    call is still nudged."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="live run", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    nudge = build_kanban_stop_nudge(messages=[], attempts=0)
    assert nudge is not None and tid in nudge


def test_nudge_is_fail_open_when_the_board_cannot_be_read(
    kanban_board: Path, clear_kanban_env, tmp_path: Path,
) -> None:
    """A board read failure must never silence a genuine nudge: the guard falls
    back to its message-history behaviour instead of letting a worker exit clean."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="unreadable board", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    # A directory can never open as a SQLite database.
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path / "not-a-db"))
    (tmp_path / "not-a-db").mkdir()
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    assert build_kanban_stop_nudge(messages=[], attempts=0) is not None
