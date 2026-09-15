"""Tests for the kanban worker turn-end stop guard.

The guard's job: a worker that ends a turn with no board-terminal tool is nudged once or
twice before the dispatcher records a protocol violation. Its failure mode (jarvis-os
t_530e25ca, t_6af13e4d): re-arming a session whose lifecycle ALREADY ended, which tells the
worker to call a terminal tool that the board will refuse, or worse a `kanban_complete` that
would approve a card another lane now owns.

Two suppression rules are pinned here:
  * a board-terminal tool in this session's history — including the review-lane hand-offs
    (`kanban_request_review` / `kanban_request_changes`), which close the run too;
  * this worker's OWN run already terminal on the board (`task_runs.outcome` set for
    `HERMES_KANBAN_RUN_ID`) — bound to run identity, not to `tasks.status`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    reset_run_outcome_cache,
    session_called_kanban_terminal,
)
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture(autouse=True)
def clear_kanban_env(monkeypatch):
    """No inherited board pins: a dispatcher-spawned test run must never read a live board."""
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_STOP_NUDGE",
        "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    # The run-outcome memo is process-global; a stale entry would mask a rule under test.
    reset_run_outcome_cache()
    return monkeypatch


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real, isolated board DB so the guard's run-outcome read hits actual rows."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _plain_text_turn() -> list[dict]:
    """A turn that ends with narration only — what the guard exists to catch."""
    return [
        {"role": "user", "content": "work kanban task"},
        {"role": "assistant", "content": "Done, the review is written up."},
    ]


def _review_handoff() -> tuple[str, int, int]:
    """Implementer hands off to review, a reviewer claims it: (task_id, run, reviewer_run)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review lane", assignee="implementer")
        kb.claim_task(conn, tid)
        implementer_run = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(
            conn, tid, summary="ready for review", expected_run_id=implementer_run,
        ) is True
        reviewer = kb.claim_review_task(conn, tid)
        assert reviewer is not None
    return tid, implementer_run, reviewer.current_run_id


# ── Existing guard contract ─────────────────────────────────────────────────


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


def test_nudge_claims_only_what_it_read(clear_kanban_env):
    """The template must not assert card state the module never read."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    nudge = build_kanban_stop_nudge(messages=_plain_text_turn())
    assert nudge is not None
    assert "is still `running`" not in nudge
    assert "is still running" not in nudge


# ── Rule 1: a board-terminal tool in the session ────────────────────────────


@pytest.mark.parametrize("tool", ["kanban_request_review", "kanban_request_changes"])
def test_review_handoff_in_session_is_terminal(clear_kanban_env, tool):
    """A review-lane hand-off closes the run exactly like complete/block."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_review")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": tool, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": tool, "tool_call_id": "1", "content": '{"ok": true}'},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is None


def test_non_terminal_tools_still_nudge(clear_kanban_env):
    """comment/heartbeat/hold are not board-terminal: the guard still fires."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_nonterminal")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "kanban_comment", "arguments": "{}"}},
                {"id": "2", "type": "function", "function": {"name": "kanban_heartbeat", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "name": "kanban_comment", "tool_call_id": "1", "content": "ok"},
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "2", "content": "ok"},
    ]
    assert session_called_kanban_terminal(messages) is False
    assert build_kanban_stop_nudge(messages=messages, attempts=0) is not None


# ── Rule 2: this worker's own run is already terminal on the board ──────────


def test_no_nudge_when_own_run_is_terminal_on_board(clear_kanban_env, board):
    """The reported repro: reviewer hands the card back, run closed, card left `ready`."""
    tid, _implementer_run, reviewer_run = _review_handoff()
    with kbc.connect() as conn:
        ok, implementer = kb.request_changes(
            conn, tid, reason="needs rework", expected_run_id=reviewer_run,
        )
        assert ok is True
        assert implementer == "implementer"
        assert kb.get_task(conn, tid).status == "ready"  # no run holds the card any more
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(reviewer_run))
    # Plain-text turn: the nudge would previously fire and demand a terminal the board refuses.
    assert build_kanban_stop_nudge(messages=_plain_text_turn(), attempts=0) is None


def test_no_nudge_when_own_run_has_outcome_even_with_no_transcript(clear_kanban_env, board):
    tid, _implementer_run, reviewer_run = _review_handoff()
    with kbc.connect() as conn:
        assert kb.request_changes(conn, tid, reason="rework", expected_run_id=reviewer_run)[0] is True
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(reviewer_run))
    assert build_kanban_stop_nudge(messages=[], attempts=0) is None


def test_nudge_still_fires_while_the_run_is_open(clear_kanban_env, board):
    """The guard's actual job must not regress: an open run with a silent turn is nudged."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="live work", assignee="implementer")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.get_task(conn, tid).status == "running"
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", tid)
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    nudge = build_kanban_stop_nudge(messages=_plain_text_turn(), attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge


# ── Fail-open on board-read failure ─────────────────────────────────────────


def test_fail_open_when_board_is_unreadable(clear_kanban_env, tmp_path):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "424242")
    clear_kanban_env.setenv("HERMES_KANBAN_DB", str(tmp_path))  # a directory, not a DB file
    nudge = build_kanban_stop_nudge(messages=_plain_text_turn(), attempts=0)
    assert nudge is not None, "an unreadable board must never silence a genuine nudge"


def test_fail_open_when_run_id_is_unknown(clear_kanban_env, board):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_RUN_ID", "999999999")
    assert build_kanban_stop_nudge(messages=_plain_text_turn(), attempts=0) is not None


def test_fail_open_when_run_id_is_absent(clear_kanban_env, board):
    """Older dispatchers export no run id: fall back to transcript-only behaviour."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    assert build_kanban_stop_nudge(messages=_plain_text_turn(), attempts=0) is not None
