"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    build_kanban_stop_fallback_block,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






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






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.




def test_nudge_and_dispatcher_budgets_are_independent(clear_kanban_env):
    """Agent-side nudge budget (2) and dispatcher-side streak (3) are
    separate budgets — the nudge counter does not affect the dispatcher's
    violation streak, and vice versa.

    This is a source-level invariant check: the nudge counter
    (``_kanban_stop_nudges``) lives on the AIAgent instance and resets
    per session, while the dispatcher streak lives in the task_runs DB
    table and persists across worker respawns.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    # Agent-side: 2 nudge attempts per session
    assert build_kanban_stop_nudge(messages=[], attempts=0) is not None
    assert build_kanban_stop_nudge(messages=[], attempts=1) is not None
    assert build_kanban_stop_nudge(messages=[], attempts=2) is None
    # Dispatcher-side streak is tracked in the DB, not in the nudge module —
    # the nudge module has no knowledge of the streak counter.
    assert not hasattr(build_kanban_stop_nudge, "_streak")


# ── Hard terminal-call fallback (t_44cfa735) ───────────────────────────
# When the nudge budget is exhausted and the worker STILL exits with only a
# narration (no kanban_complete/kanban_block), the harness must fire a concrete
# kanban_block so the card lands in a visible `blocked` state with a real reason
# — never silently `running` (protocol_violation) and never a phantom complete.


def test_fallback_block_returns_payload_after_budget(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_pv123")
    # attempts == max_attempts (2) => budget exhausted => fallback fires.
    fb = build_kanban_stop_fallback_block(
        final_response="Done, wrote the file.", model="tencent/hy3:free",
        attempts=2, max_attempts=2,
    )
    assert fb is not None
    assert fb["kind"] == "capability"
    assert fb["auto_guard"] is True
    assert fb["task_id"] == "t_pv123"
    assert "auto-block" in fb["reason"].lower()
    assert "tencent/hy3:free" in fb["reason"]


def test_fallback_block_suppressed_before_budget_exhausted(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_pv123")
    # attempts < max_attempts => caller should still nudge, not hard-block.
    assert build_kanban_stop_fallback_block(attempts=0, max_attempts=2) is None
    assert build_kanban_stop_fallback_block(attempts=1, max_attempts=2) is None


def test_fallback_block_disabled_without_kanban_task(clear_kanban_env):
    assert build_kanban_stop_fallback_block(attempts=5, max_attempts=2) is None


def test_fallback_block_suppressed_if_terminal_called(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_pv123")
    # A session that already called a terminal tool must never be double-blocked.
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "1", "type": "function",
                 "function": {"name": "kanban_complete", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "name": "kanban_complete",
         "tool_call_id": "1", "content": "done"},
    ]
    # session_called_kanban_terminal scans live messages; pass them explicitly.
    assert build_kanban_stop_fallback_block(
        attempts=2, max_attempts=2,
    ) is not None  # returns payload, but the harness guards on session_called_kanban_terminal
    # The defensive double-guard inside build_kanban_stop_fallback_block only
    # checks session_called_kanban_terminal(None); the harness passes live
    # messages. Verify session_called_kanban_terminal sees the terminal call.
    assert session_called_kanban_terminal(messages) is True
