"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    reset_run_outcome_cache,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_run_outcome_cache()
    yield monkeypatch
    reset_run_outcome_cache()


def _msgs_with_tool_call(name: str) -> list[dict]:
    """Minimal history in which the assistant invoked terminal tool ``name``."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": name, "tool_call_id": "1", "content": "ok"},
    ]


def _seed_board_run(db_path, monkeypatch, *, outcome=None) -> int:
    """Create a real board DB with one task and one run row.

    ``outcome=None`` seeds an open (live) run; any string seeds a terminal
    run the way the dispatcher closes it. Returns the run id. The DB is
    pinned via ``HERMES_KANBAN_DB`` so ``kanban_db_connect.connect()``
    inside the guard resolves to this file.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    conn = kbc.connect(db_path=db_path)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, body, status, created_at) "
            "VALUES ('t_run', 'seed', NULL, 'running', strftime('%s','now'))"
        )
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, "
            "ended_at, outcome) VALUES ('t_run', 'forge', 'running', "
            "strftime('%s','now'), ?, ?)",
            (
                None if outcome is None else int(__import__("time").time()),
                outcome,
            ),
        )
        run_id = int(cur.lastrowid or 0)
        conn.commit()
    finally:
        conn.close()
    reset_run_outcome_cache()
    return run_id


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


# ── Review-lane terminals count as board-terminal (#98107) ────────────


@pytest.mark.parametrize(
    "tool_name",
    [
        "kanban_request_review",
        "kanban_request_changes",
        "kanban_complete",
        "kanban_block",
    ],
)
def test_review_lane_terminals_suppress_nudge(clear_kanban_env, tool_name):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = _msgs_with_tool_call(tool_name)
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


def test_non_terminal_tools_still_nudge(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_xyz")
    messages = _msgs_with_tool_call("kanban_comment")
    assert session_called_kanban_terminal(messages) is False
    nudge = build_kanban_stop_nudge(messages=messages)
    assert nudge is not None


def test_heartbeat_only_still_nudges(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_hb")
    messages = _msgs_with_tool_call("kanban_heartbeat")
    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is not None


# ── Run-outcome suppression (#98750) ───────────────────────────────────


def test_run_outcome_review_requested_suppresses_nudge(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    run_id = _seed_board_run(db_path, clear_kanban_env, outcome="review_requested")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_run")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    # No terminal tool call anywhere in this session's transcript window —
    # the outcome-suppression path must still fire on its own.
    messages = [{"role": "user", "content": "hello"}]
    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages) is None


def test_run_outcome_changes_requested_suppresses_nudge(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    run_id = _seed_board_run(db_path, clear_kanban_env, outcome="changes_requested")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_run")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    messages = [{"role": "user", "content": "hello"}]
    assert build_kanban_stop_nudge(messages=messages) is None


def test_run_outcome_open_still_nudges(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    run_id = _seed_board_run(db_path, clear_kanban_env, outcome=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_run")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    messages = [{"role": "user", "content": "hello"}]
    assert build_kanban_stop_nudge(messages=messages) is not None


def test_missing_run_id_falls_back_to_transcript_only(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    _seed_board_run(db_path, clear_kanban_env, outcome="completed")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_run")
    # HERMES_KANBAN_RUN_ID intentionally left unset (older dispatcher).
    messages = [{"role": "user", "content": "hello"}]
    # No terminal tool in transcript, no run id to check outcome from ->
    # guard fails open and still nudges.
    assert build_kanban_stop_nudge(messages=messages) is not None


def test_unknown_run_id_fails_open(clear_kanban_env, tmp_path):
    db_path = tmp_path / "kanban.db"
    _seed_board_run(db_path, clear_kanban_env, outcome="completed")
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_run")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "999999")
    messages = [{"role": "user", "content": "hello"}]
    assert build_kanban_stop_nudge(messages=messages) is not None


def test_unreadable_board_fails_open(clear_kanban_env, tmp_path):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_run")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "1")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path / "does-not-exist" / "kanban.db"))
    messages = [{"role": "user", "content": "hello"}]
    # Board path doesn't exist / can't be opened meaningfully -> None outcome
    # -> nudge still fires. connect() auto-creates missing dirs/files in the
    # non-delegated-child path, so this also covers "empty schema, unknown
    # run id" rather than a raw I/O error; both must fail open.
    assert build_kanban_stop_nudge(messages=messages) is not None


def test_open_run_cache_does_not_stale_out_closure(clear_kanban_env, tmp_path):
    """None (open-run) reads must not be cached -- the run may close later.

    Regression for the #98750 defense-in-depth cache-miss staleness defect:
    a guard fire while the run is still open must not poison the memoized
    outcome for a later, same-process re-check after the run has closed.
    """
    db_path = tmp_path / "kanban.db"
    run_id = _seed_board_run(db_path, clear_kanban_env, outcome=None)
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_run")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    messages = [{"role": "user", "content": "hello"}]

    # Fire #1: run is open -> nudge fires (and must NOT poison the cache
    # with a stale None keyed to this run).
    assert build_kanban_stop_nudge(messages=messages) is not None

    # Close the run in the DB, exactly as the dispatcher does after a
    # native kanban_request_review call.
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(db_path=db_path)
    try:
        conn.execute(
            "UPDATE task_runs SET outcome = ?, ended_at = strftime('%s','now') "
            "WHERE id = ?",
            ("review_requested", run_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Fire #2: same process, run now closed on the board, transcript still
    # shows no terminal tool call (e.g. compacted out). The run-outcome
    # suppression must see the fresh non-null outcome, not the cached None
    # from fire #1.
    assert build_kanban_stop_nudge(messages=messages) is None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


def test_nudge_budget_exhausts(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_budget")
    messages = [{"role": "user", "content": "hello"}]
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is not None
    assert build_kanban_stop_nudge(messages=messages, attempts=1) is not None
    assert build_kanban_stop_nudge(messages=messages, attempts=2) is None
