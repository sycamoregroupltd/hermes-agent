"""Tests for typed block reasons + the unblock-loop breaker.

Covers the built-in fix for the kanban "blocked loop" — a worker blocks a
task, a cron unblocks it, the worker re-blocks for the same reason, repeat
forever. The fix gives ``block_task`` a typed ``kind`` and a persistent
``block_recurrences`` counter:

* ``dependency`` blocks route to ``todo`` (parent-gated, auto-resumed) and
  never enter the human ``blocked`` bucket a cron would keep unblocking.
* ``needs_input`` / ``capability`` / un-typed blocks land in ``blocked``;
  each same-cause re-block after an unblock increments ``block_recurrences``,
  and at ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
* ``unblock_task`` deliberately does NOT reset ``block_recurrences`` (the
  amnesia that let the loop run unbounded).
* A successful ``complete_task`` resets the loop memory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code.

    Mirrors the helper in test_kanban_db.py so crash fixtures can simulate a
    worker subprocess exiting non-zero without spawning a real process.
    """
    return code << 8


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    """Create a task and drive it to ``running`` so block_task can act."""
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    claimed = kb.claim_task(conn, tid, claimer="worker")
    assert claimed is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


# ---------------------------------------------------------------------------
# Loop breaker
# ---------------------------------------------------------------------------










def test_block_loop_detected_event_emitted(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        kb.block_task(conn, tid, reason="x", kind="capability")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="x", kind="capability")
        events = [e for e in kb.list_events(conn, tid)
                  if e.kind == "block_loop_detected"]
        assert events, "expected a block_loop_detected event"
        payload = events[-1].payload or {}
        assert payload.get("recurrences") == 2
        assert payload.get("kind") == "capability"


# ---------------------------------------------------------------------------
# Dependency routing
# ---------------------------------------------------------------------------


def test_dependency_then_parent_done_promotes(kanban_home: Path) -> None:
    """A dependency-parked child becomes ready once its parent completes."""
    with kb.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = _running_task(conn, title="child")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        kb.block_task(conn, child, reason="wait", kind="dependency")
        assert kb.get_task(conn, child).status == "todo"
        # Finish the parent, then let recompute_ready run.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        kb.claim_task(conn, parent, claimer="worker")
        kb.complete_task(conn, parent, result="done")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"


# ---------------------------------------------------------------------------
# Completion resets loop memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Validation + back-compat
# ---------------------------------------------------------------------------


def test_invalid_kind_rejected(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        with pytest.raises(ValueError):
            kb.block_task(conn, tid, reason="x", kind="bogus")


def test_block_without_kind_is_backward_compatible(kanban_home: Path) -> None:
    """Existing callers that pass no kind keep the old single-block behaviour."""
    with kb.connect_closing() as conn:
        tid = _running_task(conn)
        assert kb.block_task(conn, tid, reason="legacy")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked"
        assert t.block_kind is None


# ---------------------------------------------------------------------------
# Regression: circuit-breaker auto-block must NEVER emit needs_input
# (t_ee20a992). Technical-failure recurrences (provider/pid/loop crashes) are
# retryable and must be typed ``transient`` (or ``dependency``), so routing/
# escalation cannot collapse them into a human-input gate. ``needs_input`` is
# reserved for an explicit worker ``kanban_block(kind="needs_input")`` call.
# ---------------------------------------------------------------------------


def _drive_crash_through_breaker(conn, monkeypatch, error_substring: str) -> str:
    """Reap a dead-pid worker whose exit error contains ``error_substring``.

    Mirrors the real ``detect_crashed_workers`` funnel: the worker exits with
    a generic non-zero code (NOT the rate-limit sentinel, NOT a clean rc=0
    protocol violation), so the breaker increments the failure counter; after
    ``DEFAULT_FAILURE_LIMIT`` (== 2) trips, ``_record_task_failure`` stamps a
    typed ``block_kind`` via ``_auto_block_kind_for``. Returns the task id.
    """
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    host = _kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title=f"crash-{error_substring[:12]}", assignee="a")
    for i in range(_kb.DEFAULT_FAILURE_LIMIT):
        pid = 51000 + i
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=? "
            "WHERE id=?",
            (pid, f"{host}:w{i}", tid),
        )
        conn.commit()
        # Generic non-zero exit; the error text is what carries the failure
        # class. The classifier (lazy-imported inside _auto_block_kind_for)
        # reads it via _failure_class_for_error.
        _kb._record_worker_exit(pid, _exited_status(1))
        # Stamp the error the breaker will see (simulates the reap registry
        # surfacing the crash text on the next trip).
        conn.execute(
            "UPDATE tasks SET last_failure_error=? WHERE id=?",
            (f"pid {pid} not alive — {error_substring}", tid),
        )
        conn.commit()
        kb.detect_crashed_workers(conn)
    return tid


def test_provider_error_auto_block_is_transient_not_needs_input(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider/API failure recurrence must auto-block as ``transient``."""
    with kb.connect_closing() as conn:
        tid = _drive_crash_through_breaker(conn, monkeypatch, "RateLimitError")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked", f"breaker should trip, got {t.status}"
        assert t.block_kind == "transient", (
            f"provider_error recurrence must be transient, got {t.block_kind!r}"
        )
        assert t.block_kind != "needs_input", (
            "REGRESSION: breaker must never auto-emit needs_input"
        )


def test_pid_crash_auto_block_is_transient_not_needs_input(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker pid crash recurrence must auto-block as ``transient``."""
    with kb.connect_closing() as conn:
        tid = _drive_crash_through_breaker(conn, monkeypatch, "killed by signal")
        t = kb.get_task(conn, tid)
        assert t.status == "blocked", f"breaker should trip, got {t.status}"
        assert t.block_kind == "transient", (
            f"pid crash recurrence must be transient, got {t.block_kind!r}"
        )
        assert t.block_kind != "needs_input", (
            "REGRESSION: breaker must never auto-emit needs_input"
        )


def test_block_loop_detected_auto_block_is_dependency_not_needs_input(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-block recurrence (block_loop_detected) routes to ``dependency``."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="loop", assignee="a")
        host = _kb._claimer_id().split(":", 1)[0]
        # Pre-set a block recurrence >= BLOCK_RECURRENCE_LIMIT so that, once
        # the breaker trips, _auto_block_kind_for classifies the trip as a
        # loop-detected re-block and routes it to ``dependency``.
        conn.execute(
            "UPDATE tasks SET status='running', worker_pid=?, claim_lock=?, "
            "block_recurrences=?, last_failure_error=? WHERE id=?",
            (52000, f"{host}:w", _kb.BLOCK_RECURRENCE_LIMIT,
             "pid 52000 not alive — transient crash", tid),
        )
        conn.commit()
        # Drive the breaker to trip: each crash increments consecutive_failures
        # until DEFAULT_FAILURE_LIMIT is reached.
        for i in range(_kb.DEFAULT_FAILURE_LIMIT):
            pid = 52000 + i
            conn.execute(
                "UPDATE tasks SET status='running', worker_pid=?, claim_lock=? "
                "WHERE id=?",
                (pid, f"{host}:w{i}", tid),
            )
            conn.commit()
            _kb._record_worker_exit(pid, _exited_status(1))
            kb.detect_crashed_workers(conn)
        t = kb.get_task(conn, tid)
        assert t.status == "blocked", f"breaker should trip, got {t.status}"
        assert t.block_kind == "dependency", (
            f"loop-detected recurrence must be dependency, got {t.block_kind!r}"
        )
        assert t.block_kind != "needs_input"
