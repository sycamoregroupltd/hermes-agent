"""Regression tests for post-fix blocked-card dispatch guard (t_1f0549ec).

Tests the _has_unresolved_block() helper added at kanban_db.py line 4352
and the pre-claim guard in claim_task() that rejects claims on tasks with
unresolved sticky blocks.

The soak monitor discovered 5 violations where a card was legitimately set
to 'ready', then immediately re-blocked by a worker, and the dispatcher
still dispatched it — creating a blocked→claimed gap without unblock.

These tests verify:

* _has_unresolved_block correctly detects blocks that follow an unblock.
* _has_unresolved_block returns False for cards with no block events.
* _has_unresolved_block treats promoted/promoted_manual as clear events.
* claim_task() rejects claims on tasks with unresolved blocks.
* claim_task() succeeds on tasks whose last control event is unblocked.
* claim_task() succeeds on cards with no block history at all.
* The full re-block race is now handled: ready → block → claim rejected.

See: jarvis-os/t_1f0549ec (post-fix blocked-card-soak regression).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import sqlite3

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# _has_unresolved_block unit tests
# ---------------------------------------------------------------------------


def test_has_unresolved_block_detects_block_without_clear(kanban_home: Path) -> None:
    """A task with only a 'blocked' event and no clear event returns True."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test")
        # Manually insert a blocked event (simulating kanban_block call)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', '{\"reason\":\"review-required\"}', ?)",
            (tid, int(time.time())),
        )
        conn.commit()
        assert kb._has_unresolved_block(conn, tid) is True


def test_has_unresolved_block_false_when_no_blocks(kanban_home: Path) -> None:
    """A task with no block/unlock/promote events returns False."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="fresh task")
        assert kb._has_unresolved_block(conn, tid) is False


def test_has_unresolved_block_false_after_unblock(kanban_home: Path) -> None:
    """After a proper unblock, the guard returns False."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test")
        now = int(time.time())
        # Block
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now),
        )
        # Unblock
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'unblocked', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()
        assert kb._has_unresolved_block(conn, tid) is False


def test_has_unresolved_block_false_after_promote(kanban_home: Path) -> None:
    """A promoted event also clears the unresolved block."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'promoted', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()
        assert kb._has_unresolved_block(conn, tid) is False


def test_has_unresolved_block_true_reblock_after_unblock(kanban_home: Path) -> None:
    """The core violation pattern: block → unblock → block returns True."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test re-block race")
        now = int(time.time())
        # First block
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now),
        )
        # Legitimate unblock
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'unblocked', NULL, ?)",
            (tid, now + 1),
        )
        # Re-block (the problematic sequence)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now + 2),
        )
        conn.commit()
        assert kb._has_unresolved_block(conn, tid) is True


def test_has_unresolved_block_false_latest_is_unblocked(kanban_home: Path) -> None:
    """When the latest control event is 'unblocked', returns False
    (no blocking event exists after any clear)."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="test")
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'unblocked', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()
        # Latest event is unblocked → no unresolved block
        assert kb._has_unresolved_block(conn, tid) is False


# ---------------------------------------------------------------------------
# claim_task pre-claim guard tests
# ---------------------------------------------------------------------------


def test_claim_task_rejected_on_unresolved_block(kanban_home: Path) -> None:
    """claim_task must return None when the task has an unresolved block,
    even if status='ready'. This is the core fix for t_1f0549ec."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="re-block guard")
        now = int(time.time())
        # Set status to ready, insert a block event
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?",
            (tid,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', '{\"reason\":\"needs review\"}', ?)",
            (tid, now),
        )
        conn.commit()
        # Claim should be rejected
        result = kb.claim_task(conn, tid)
        assert result is None, "claim_task must reject blocked->ready races"
        # Verify blocked_dispatch_attempt event was emitted
        evt = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind = 'blocked_dispatch_attempt' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        assert evt is not None, "blocked_dispatch_attempt event must be recorded"


def test_claim_task_succeeds_after_proper_unblock(kanban_home: Path) -> None:
    """After a block followed by a legitimate unblock, claim_task succeeds."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="proper unblock path")
        now = int(time.time())
        # Insert block + unblock pair
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'unblocked', NULL, ?)",
            (tid, now + 1),
        )
        conn.commit()
        # Status should be ready (from init or explicit set)
        result = kb.claim_task(conn, tid)
        assert result is not None, "claim_task must succeed after proper unblock"
        assert get_status(conn, tid) == "running"


def test_claim_task_succeeds_on_fresh_task(kanban_home: Path) -> None:
    """A fresh task with no block history can always be claimed."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="fresh task claim")
        result = kb.claim_task(conn, tid)
        assert result is not None
        assert get_status(conn, tid) == "running"


def test_full_re_block_race_pattern_rejected(kanban_home: Path) -> None:
    """Simulates the exact t_1f0549ec violation pattern:
    
    1. Task starts as ready
    2. Worker calls kanban_block (status -> blocked)
    3. Dispatcher ticks: recompute_ready sets status -> ready (legit)
    4. Worker calls kanban_block again (re-block)
    5. Next tick: claim_task is called with status=ready but unresolved block
    6. Guard MUST reject this
    
    Before the fix, step 6 would succeed, creating the soaked violation.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="race pattern")
        kb.recompute_ready(conn)
        now = int(time.time())
        
        # Step 2: First block via direct event (kanban_block)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now),
        )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        conn.commit()
        
        # Step 3: Dispatcher ticks and promotes (parent-free, legit)
        promoted = kb.recompute_ready(conn)
        assert promoted >= 0  # may promote or skip based on parents
        cur_status = get_status(conn, tid)
        
        # Step 4: Worker re-blocks
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', NULL, ?)",
            (tid, now + 2),
        )
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        conn.commit()
        
        # Step 5-6: Another dispatcher tick promotes, then claim is attempted
        kb.recompute_ready(conn)
        # Now try to claim — should be REJECTED because block > unblock
        result = kb.claim_task(conn, tid)
        assert result is None, (
            "claim_task must reject when re-block has no subsequent unblock"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_status(conn: "sqlite3.Connection", task_id: str) -> str:
    """Return the current status column for a task."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else ""
