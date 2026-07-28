"""Regression tests for t_jarvis_autopromote_20260728 — P1 dispatch bug.

A ``blocked`` card must NEVER be claimed/dispatched by the auto-dispatcher,
for any block kind.  Before this fix, ``recompute_ready`` promoted
``blocked -> ready`` whenever ``all(parents done)`` was vacuously True (a
task with no parents), so parent-free blocked cards — e.g. Frank-only A3
gates created via ``create_task(initial_status='blocked')`` — were
re-claimed on every dispatcher tick, defeating every gate in the system.

These tests pin the contract:

* A parent-free ``blocked`` card (Frank-only A3 gate) survives arbitrary
  dispatcher ticks — it is never promoted, never claimed.
* A ``blocked`` card with all-done parents is NOT auto-promoted.
* An explicit ``unblock_task`` is the ONLY legitimate exit from ``blocked``.
* A full ``dispatch_once`` tick does not claim a ``blocked`` card, and an
  audit ``blocked`` event is the only ``blocked``-related event recorded.
* The dependency-block routing path (``block_task(kind='dependency')`` ->
  ``todo``) still auto-promotes once the parent completes — the intended
  recovery path is preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
# Parent-free blocked card (Frank-only A3 gate) must never auto-promote
# ---------------------------------------------------------------------------


def test_parent_free_blocked_card_never_autopromotes(kanban_home: Path) -> None:
    """A standalone ``blocked`` card with no parents (the exact shape of a
    Frank-only A3 gate, or ``create_task(initial_status='blocked')``) must
    stay blocked across an arbitrary number of dispatcher ticks.  Before the
    fix, ``recompute_ready`` flipped it to ``ready`` on the next tick because
    ``all()`` over an empty parent list is ``True``."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Frank-only A3 gate", initial_status="blocked"
        )
        assert kb.get_task(conn, tid).status == "blocked"

        for _ in range(10):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, "parent-free blocked card must not auto-promote"
            assert kb.get_task(conn, tid).status == "blocked"


def test_create_task_initial_status_blocked_is_sticky(kanban_home: Path) -> None:
    """Reproduces the exact t_a9819a57 / t_jarvis_autopromote_20260728 defect:
    a parent-free card created with ``initial_status='blocked'`` (the shape
    of a Frank-only A3 gate) must be sticky — ``recompute_ready`` must not
    promote it, and a full ``dispatch_once`` tick must not claim it.

    This is the regression guard: before the fix, ``create_task`` set
    ``status='blocked'`` but emitted no ``blocked`` event, so
    ``_has_sticky_block`` returned False and the vacuous parent check
    promoted the card to ``ready`` on the next tick."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="Frank-only A3 gate", initial_status="blocked"
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"

        # The fix: a 'blocked' lifecycle event is emitted so the gate is
        # durable (never auto-promoted by recompute_ready).
        kinds = [e["kind"] for e in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,)
        )]
        assert "blocked" in kinds

        # No amount of dispatcher ticks promotes or claims it.
        for _ in range(10):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, tid).status == "blocked"
        assert len(kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 0).spawned) == 0
        assert kb.get_task(conn, tid).status == "blocked"


def test_blocked_card_with_done_parents_never_autopromotes(kanban_home: Path) -> None:
    """Even when every parent is ``done``/``archived``, a worker/operator
    ``blocked`` child must stay blocked.  This is the most dangerous
    false-positive because the parent-completion path was the one
    ``recompute_ready`` was designed for."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.complete_task(conn, parent, result="ok")
        assert kb.get_task(conn, parent).status == "done"

        # Block the child as a worker/operator gate (emits a 'blocked' event).
        kb.claim_task(conn, child)
        kb.block_task(
            conn, child,
            reason="review-required: child needs sign-off",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        )
        assert kb.get_task(conn, child).status == "blocked"

        for _ in range(5):
            assert kb.recompute_ready(conn) == 0
            assert kb.get_task(conn, child).status == "blocked"


# ---------------------------------------------------------------------------
# dispatch_once must not claim a blocked card
# ---------------------------------------------------------------------------


def test_dispatch_once_does_not_claim_blocked_card(kanban_home: Path) -> None:
    """End-to-end: a full dispatcher tick must leave a ``blocked`` card
    unclaimed.  The only ``blocked``-related event must be the original gate,
    not a claim."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="gated card", assignee="platform-builder",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        events_before = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,)
        ).fetchall()

        result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 0)
        assert len(result.spawned) == 0, "blocked card must not be claimed"

        task = kb.get_task(conn, tid)
        assert task.status == "blocked", "blocked card must not be claimed"
        assert task.claim_lock is None
        assert task.current_run_id is None

        events_after = conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (tid,)
        ).fetchall()
        # No new 'claimed' / 'promoted' events appeared.
        assert [e["kind"] for e in events_after] == [e["kind"] for e in events_before]


# ---------------------------------------------------------------------------
# unblock_task is the only legitimate exit
# ---------------------------------------------------------------------------


def test_unblock_task_is_the_only_exit(kanban_home: Path) -> None:
    """``unblock_task`` moves a ``blocked`` card to ``ready`` (so it then
    becomes claimable); ``recompute_ready`` alone must never do so."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="gated", initial_status="blocked"
        )
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, tid).status == "blocked"

        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"


# ---------------------------------------------------------------------------
# Intended recovery path preserved: dependency blocks route to todo
# ---------------------------------------------------------------------------


def test_dependency_block_still_recovers_via_todo(kanban_home: Path) -> None:
    """``block_task(kind='dependency')`` routes the child to ``todo`` (not
    ``blocked``), so ``recompute_ready`` still promotes it once the parent
    completes.  This is the recovery path the fix must NOT break."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent])
        kb.claim_task(conn, child)
        kb.block_task(
            conn, child, kind="dependency",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        )
        assert kb.get_task(conn, child).status == "todo"

        # Parent not done yet — not promoted.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"

        # Completing the parent triggers an internal recompute_ready that
        # promotes the dependency-gated child to ready.
        kb.complete_task(conn, parent, result="ok")
        assert kb.get_task(conn, child).status == "ready"
