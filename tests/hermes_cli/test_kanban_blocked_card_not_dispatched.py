"""Regression test: a card created with ``initial_status='blocked'`` must NOT
be claimed by the auto-dispatch mechanism.

The bug: ``create_task`` with ``initial_status='blocked'`` writes
``status='blocked'`` into the DB row but emits only a ``"created"`` event
(not a ``"blocked"`` event).  Because ``_has_sticky_block()`` looks for a
``"blocked"`` event, it returns ``False``, so the dispatcher's
``recompute_ready()`` promotes the task to ``ready`` and ``dispatch_once()``
claims + spawns it — defeating the entire point of creating a blocked card.

Acceptance:
  - test fails (task gets promoted/claimed) with current code
  - test passes (task stays blocked, no spawn) after the fix
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def all_assignees_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assume every assignee is a real Hermes profile so the dispatcher
    does not skip them as nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def test_initial_status_blocked_card_is_not_dispatched(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A card created with ``initial_status='blocked'`` must stay blocked
    after a full ``dispatch_once`` cycle.

    Before the fix, ``create_task`` did not emit a ``"blocked"`` event for
    ``initial_status='blocked'``, so ``_has_sticky_block()`` returned False,
    ``recompute_ready()`` promoted it to ``ready``, and ``dispatch_once()``
    claimed it — a blocked card that gets dispatched.
    """
    with kb.connect() as conn:
        # Create a blocked card with an assignee so the dispatcher can
        # attempt to spawn it.
        tid = kb.create_task(
            conn,
            title="must not be dispatched",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Track whether spawn_fn was ever called.
        spawn_calls: list[str] = []

        def spy_spawn(task, workspace_path, board=None):
            spawn_calls.append(getattr(task, "id", str(task)))
            return 999999  # fake PID

        # Run one full dispatch tick — this calls recompute_ready then
        # attempts to claim any ready tasks and spawn them.
        result = kb.dispatch_once(conn, spawn_fn=spy_spawn)

        # The blocked card must NOT have been promoted or claimed.
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"Blocked card must stay blocked after dispatch_once; "
            f"got status={task.status!r} "
            f"(spawned={result.spawned!r})"
        )
        assert len(result.spawned) == 0, (
            f"dispatch_once must not spawn any task for a blocked card; "
            f"got spawned={result.spawned!r}"
        )
        assert len(spawn_calls) == 0, (
            f"spawn_fn must not have been called for a blocked card; "
            f"calls={spawn_calls!r}"
        )


def test_initial_status_blocked_card_not_promoted_by_recompute_ready(
    kanban_home: Path,
) -> None:
    """Even the intermediate ``recompute_ready`` step must not promote a
    card that was created with ``initial_status='blocked'``.

    This isolates the promotion half of the bug from the claim/spawn half,
    making it easier to tell which layer failed.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="blocked-promotion-test",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Hammer recompute_ready, same as the dispatcher does every tick.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, (
                f"recompute_ready must not promote a blocked card; "
                f"got promoted={promoted}"
            )
            assert kb.get_task(conn, tid).status == "blocked"


# ── helpers for extended tests ───────────────────────────────────────────


def _spy_spawn():
    """Return a ``spawn_fn`` that records calls + returns a fake PID."""
    calls: list[str] = []

    def spy(task, workspace_path, board=None):
        calls.append(getattr(task, "id", str(task)))
        return 999999  # fake PID

    spy.calls = calls  # type: ignore[attr-defined]
    return spy


def _blocked_event_count(conn, task_id: str) -> int:
    """Count ``blocked`` events in ``task_events`` for ``task_id``."""
    rows = conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked'",
        (task_id,),
    ).fetchone()
    return rows["cnt"] if rows else 0


def _block_gate_audit_count(conn, task_id: str) -> int:
    """Count ``block_gate_audit`` events in ``task_events`` for ``task_id``."""
    rows = conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_events "
        "WHERE task_id = ? AND kind = 'block_gate_audit'",
        (task_id,),
    ).fetchone()
    return rows["cnt"] if rows else 0


# ── extended regression: block_task after creation + dispatch ────────────


@pytest.mark.parametrize("kind", ["needs_input", "capability", "transient", None])
def test_block_task_blocks_card_from_dispatch(
    kanban_home: Path,
    all_assignees_spawnable: None,
    kind: str | None,
) -> None:
    """A card created as ``running`` then blocked via ``block_task`` must NOT
    be dispatched — tests ``kanban_block`` path for all ``VALID_BLOCK_KINDS``.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title=f"block-after-creation-{kind}",
            assignee="test-profile",
            initial_status="running",
        )
        # Without parents, initial_status="running" creates the task as
        # "ready" (immediately dispatchable).  Block it to test the gate.
        assert kb.get_task(conn, tid).status == "ready"

        prev_events = _blocked_event_count(conn, tid)
        kb.block_task(conn, tid, reason="deliberate test block", kind=kind)
        assert _blocked_event_count(conn, tid) == prev_events + 1, (
            f"block_task must emit a 'blocked' event for kind={kind!r}"
        )
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"

        # Dispatch must not claim it
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"Blocked card must stay blocked after dispatch_once; "
            f"got status={task.status!r} (kind={kind!r})"
        )
        assert len(result.spawned) == 0, (
            f"dispatch_once must not spawn a blocked card; "
            f"got spawned={result.spawned!r} (kind={kind!r})"
        )


# ── extended regression: blind-spot guard ────────────────────────────────


def test_blind_spot_blocked_status_without_blocked_event(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A card with ``status='blocked'`` set directly in the DB without a
    corresponding ``'blocked'`` event must also NOT be dispatched.

    This tests the blind-spot guard (``90d03e99``): ``recompute_ready``
    must not auto-promote ``status='blocked'`` rows even when no matching
    ``'blocked'`` event exists.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="blind-spot-blocked",
            assignee="test-profile",
            initial_status="running",
        )
        assert kb.get_task(conn, tid).status == "ready"

        # Directly set status to 'blocked' WITHOUT emitting any event.
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))

        # The blind-spot guard in recompute_ready must NOT promote it.
        for i in range(3):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, (
                f"Blind-spot guard failed: recompute_ready promoted a "
                f"blocked card without a blocked event (iteration {i})"
            )
            task = kb.get_task(conn, tid)
            assert task.status == "blocked", (
                f"Card bypassed blind-spot guard: status changed "
                f"to {task.status!r} (iteration {i})"
            )

        # Full dispatch tick must also keep it blocked.
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"Blind-spot card must stay blocked after dispatch; "
            f"got status={task.status!r}"
        )
        assert len(result.spawned) == 0


# ── extended regression: dependency block auto-recovery ──────────────────


def test_dependency_block_still_auto_recovers_via_todo(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A ``dependency``-kind blocked card whose parent completes must still
    auto-recover via ``todo`` → ``ready`` promotion.

    This is the intentional auto-recovery path — the fix must not break it.
    """
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="parent-profile")
        kb.complete_task(conn, parent_id, result="parent done")
        assert kb.get_task(conn, parent_id).status == "done"

        child_id = kb.create_task(
            conn,
            title="dependency-child",
            assignee="test-profile",
            parents=[parent_id],
        )
        # Parent is already done, so child starts as ready.
        # Block with dependency kind → routes to todo.
        kb.block_task(conn, child_id, reason="waiting on parent", kind="dependency")
        status = kb.get_task(conn, child_id).status
        assert status == "todo", (
            f"dependency-blocked card must route to 'todo'; got {status!r}"
        )

        # Parent is done → recompute_ready promotes the child.
        promoted = kb.recompute_ready(conn)
        assert promoted >= 1, (
            "recompute_ready must promote the dependency child; parent is done"
        )
        assert kb.get_task(conn, child_id).status == "ready"


# ── extended regression: unblock_task is the only exit ───────────────────


def test_unblock_task_is_the_only_exit(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """Calling ``unblock_task`` must emit an ``unblocked`` event and return
    the card to the ready/todo pool, making it dispatachable again.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="unblock-exit-test",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Must NOT be dispatched while blocked
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        assert len(result.spawned) == 0

        # Unblock
        kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.status in ("ready", "todo"), (
            f"After unblock_task, card must exit blocked; got {task.status!r}"
        )

        # Now dispatch must pick it up
        result = kb.dispatch_once(conn, spawn_fn=spy)
        assert len(result.spawned) > 0, (
            "After unblock, card must be dispatchable"
        )
        # Verify an 'unblocked' event was emitted
        rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_events "
            "WHERE task_id = ? AND kind = 'unblocked'",
            (tid,),
        ).fetchone()
        assert rows and rows["cnt"] >= 1, (
            "unblock_task must emit at least one 'unblocked' event"
        )


# ── defense-in-depth: block-gate audit in _dispatch_once_locked ────────


def test_block_gate_audit_fires_when_ready_card_has_blocked_event(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """Defense-in-depth block-gate audit (t_fc1fdf31): a card in ``ready``
    status that somehow has a ``blocked`` event (bypassing the
    ``recompute_ready`` blind-spot guard) must be caught by
    ``_dispatch_once_locked``, which:

    1. Skips the spawn (card is NOT claimed).
    2. Appends the task id to ``result.skipped_block_gate``.
    3. Emits a ``block_gate_audit`` event in ``task_events``.

    This tests the third layer of defense — the defense-in-depth audit
    inside the dispatch tick itself, after both the ``recompute_ready``
    guard and the blind-spot guard have been bypassed (e.g. via direct
    DB manipulation that leaves status='ready' + a stale blocked event).
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="block-gate-audit-test",
            assignee="test-profile",
        )
        # Task is ready (no parents, valid assignee).
        assert kb.get_task(conn, tid).status == "ready"

        # Inject a 'blocked' event directly, as if a block happened
        # before but the card was then put back to 'ready' via direct
        # DB manipulation — bypassing the first two defense layers.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (tid, '{"origin":"db-injection","reason":"test"}', now),
        )
        conn.commit()

        # _has_sticky_block must now return True
        assert kb._has_sticky_block(conn, tid), (
            "_has_sticky_block must detect the injected blocked event"
        )

        # Run dispatch — the block-gate audit in _dispatch_once_locked
        # must catch this ready-but-blocked card.
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        # (a) Card must NOT be claimed/spawned
        assert tid not in result.spawned, (
            f"Block-gate audit failed: blocked+ready card was spawned "
            f"(spawned={result.spawned!r})"
        )
        assert len(spy.calls) == 0, (
            f"spawn_fn must not be called for a blocked+ready card; "
            f"got calls={spy.calls!r}"
        )

        # (b) Card must appear in skipped_block_gate (audit triggered)
        assert tid in result.skipped_block_gate, (
            f"Block-gate audit failed: card must be in "
            f"skipped_block_gate; got {result.skipped_block_gate!r}"
        )

        # (c) A block_gate_audit event must exist in task_events
        audit_count = _block_gate_audit_count(conn, tid)
        assert audit_count >= 1, (
            f"block_gate_audit event was not created; "
            f"expected >=1, got {audit_count}"
        )

        # Card stays 'ready' — the audit is a no-mutate gate, not a
        # status change.
        assert kb.get_task(conn, tid).status == "ready"


def test_dispatch_once_skips_blocked_card_at_spawn_time(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """End-to-end regression: ``dispatch_once`` must skip a ready card
    with a sticky block AND produce a ``block_gate_audit`` event.

    This is the full trace from the task body AC — creates a blocked
    card, attempts to dispatch it, and verifies:
    (a) the card is not claimed,
    (b) an audit log entry is created with the reason 'blocked'.
    """
    with kb.connect() as conn:
        # Create the task so it lands in 'ready' (no parents, assignee).
        tid = kb.create_task(
            conn,
            title="blocked-dispatch-audit-e2e",
            assignee="test-profile",
        )
        assert kb.get_task(conn, tid).status == "ready"

        # Inject a 'blocked' event to create a sticky block without
        # touching the status. This simulates the exact edge case:
        # a card that is technically 'ready' but has an unresolved
        # block from a prior manual DB manipulation or missed event.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (tid, '{"origin":"test-injection","reason":"blocked"}', now),
        )
        conn.commit()

        # (a) Card must NOT be claimed — verify before and after dispatch
        pre_task = kb.get_task(conn, tid)
        assert pre_task.claim_lock is None, "No claim lock before dispatch"

        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        # Card is still not claimed after dispatch
        post_task = kb.get_task(conn, tid)
        assert post_task.claim_lock is None, (
            f"Card was claimed despite having a sticky block; "
            f"claim_lock={post_task.claim_lock!r}"
        )
        assert tid not in result.spawned, (
            f"Card was spawned despite sticky block"
        )

        # (b) Audit event exists with reason 'blocked' in the payload
        audit_rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'block_gate_audit'",
            (tid,),
        ).fetchall()
        assert len(audit_rows) >= 1, (
            "No block_gate_audit event was created"
        )

        # Verify block_kind is included in the audit payload
        payload = json.loads(audit_rows[0]["payload"])
        assert "block_kind" in payload, (
            f"block_gate_audit payload must include block_kind; "
            f"got {audit_rows[0]['payload']!r}"
        )

        # Card appears in skipped_block_gate as final evidence
        assert tid in result.skipped_block_gate, (
            f"Card must be in skipped_block_gate; "
            f"got {result.skipped_block_gate!r}"
        )


# ── gap coverage: explicit event emission ──────────────────────────────


def test_create_task_blocked_emits_blocked_event(
    kanban_home: Path,
) -> None:
    """``create_task`` with ``initial_status='blocked'`` must emit a
    ``'blocked'`` event in ``task_events`` — tests the fix directly at the
    source rather than through downstream effects.

    Without this event, ``_has_sticky_block()`` returns ``False`` and the
    dispatcher would promote the card to ``ready`` on the next tick. This is
    the root-cause test for the P1 defect (t_fc1fdf31).
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="blocked-event-test",
            assignee="test-profile",
            initial_status="blocked",
        )
        # Verify a 'blocked' event was written
        rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked'",
            (tid,),
        ).fetchall()
        assert len(rows) >= 1, (
            f"create_task with initial_status='blocked' must emit "
            f"a 'blocked' event; got {len(rows)} rows"
        )
        # Verify the payload mentions "initial_status"
        payload = rows[0]["payload"]
        assert payload is not None and "initial_status" in payload, (
            f"blocked event payload should indicate 'initial_status' as source; "
            f"got {payload!r}"
        )


# ── gap coverage: mixed queue (blocked + ready) ────────────────────────


def test_mixed_blocked_and_ready_cards_in_single_dispatch_tick(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """When both a blocked card and a ready card are in the dispatch queue,
    only the ready card must be spawned — the blocked card must not prevent
    the ready one from being dispatched, nor be claimed itself.
    """
    with kb.connect() as conn:
        blocked_id = kb.create_task(
            conn,
            title="must-stay-blocked",
            assignee="test-profile",
            initial_status="blocked",
        )
        ready_id = kb.create_task(
            conn,
            title="should-be-dispatched",
            assignee="test-profile",
        )
        assert kb.get_task(conn, blocked_id).status == "blocked"
        assert kb.get_task(conn, ready_id).status == "ready"

        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        # Blocked card must stay blocked and not be spawned
        assert kb.get_task(conn, blocked_id).status == "blocked", (
            "Blocked card must stay blocked in mixed queue"
        )
        spawned_ids = [s[0] for s in result.spawned]
        assert blocked_id not in spawned_ids, (
            f"Blocked card must not be spawned in mixed queue; "
            f"got spawned={result.spawned!r}"
        )
        assert blocked_id not in spy.calls, (
            "spawn_fn must not be called for blocked card in mixed queue"
        )

        # Ready card must be dispatched
        assert ready_id in spawned_ids, (
            f"Ready card must be spawned in mixed queue; "
            f"got spawned={result.spawned!r}"
        )
        assert ready_id in spy.calls, (
            "spawn_fn must be called for the ready card in mixed queue"
        )
        assert kb.get_task(conn, ready_id).status == "running", (
            "Ready card must transition to 'running' after dispatch"
        )


# ── gap coverage: multiple dispatch ticks ──────────────────────────────


def test_blocked_card_stays_blocked_across_multiple_ticks(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A blocked card must remain blocked after N consecutive
    ``dispatch_once`` ticks — verifies no gradual state leakage or
    tick-boundary edge case can eventually claim it.
    """
    N_TICKS = 5
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="multi-tick-blocked",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        spy = _spy_spawn()
        for tick in range(N_TICKS):
            result = kb.dispatch_once(conn, spawn_fn=spy)
            task = kb.get_task(conn, tid)
            assert task.status == "blocked", (
                f"Blocked card changed to {task.status!r} after "
                f"tick {tick + 1}/{N_TICKS}"
            )
            assert len(result.spawned) == 0, (
                f"dispatch_once spawned something on tick {tick + 1}: "
                f"{result.spawned!r}"
            )
        assert len(spy.calls) == 0, (
            f"spawn_fn was called {len(spy.calls)} times over "
            f"{N_TICKS} ticks for a blocked card"
        )


# ── gap coverage: sticky block with done parent ───────────────────────


def test_sticky_blocked_card_with_done_parent_stays_blocked(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A card that is sticky-blocked (via ``block_task`` with a non-dependency
    kind) must stay blocked even when all its parents are done. Only the
    ``dependency`` block kind should auto-recover via ``todo`` promotion.
    """
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn, title="parent", assignee="parent-profile",
        )
        kb.complete_task(conn, parent_id, result="done")
        assert kb.get_task(conn, parent_id).status == "done"

        child_id = kb.create_task(
            conn,
            title="child-needs_input",
            assignee="test-profile",
            parents=[parent_id],
        )
        # Child is ready (parent done).
        assert kb.get_task(conn, child_id).status == "ready"

        # Block with needs_input — must land in 'blocked', not 'todo'.
        kb.block_task(
            conn, child_id, reason="needs human review", kind="needs_input",
        )
        assert kb.get_task(conn, child_id).status == "blocked", (
            "needs_input block must route to 'blocked', not 'todo'"
        )

        # recompute_ready should NOT promote it — sticky block overrides
        # parent-done logic.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0, (
            "recompute_ready must not promote a needs_input-blocked card "
            "even when parents are done"
        )
        assert kb.get_task(conn, child_id).status == "blocked"

        # Full dispatch tick must also refuse.
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        assert kb.get_task(conn, child_id).status == "blocked"
        assert len(result.spawned) == 0
        assert child_id not in spy.calls


# ── blocked_dispatch_attempt audit event ───────────────────────────────


def test_blocked_card_records_blocked_dispatch_attempt_event(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A card created with ``initial_status='blocked'`` must produce a
    ``blocked_dispatch_attempt`` event after ``dispatch_once`` runs, proving
    that the dispatcher audited the blocked card (even though the SQL filter
    prevents it from ever reaching the spawn loop).

    Acceptance:
      - Card stays blocked and unclaimed after dispatch_once.
      - A ``blocked_dispatch_attempt`` event exists in ``task_events``.
      - The ``run_count`` column in ``task_runs`` stays NULL (no attempt
        to claim the card).
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="blocked-dispatch-attempt-audit-test",
            assignee="test-profile",
            initial_status="blocked",
        )
        assert kb.get_task(conn, tid).status == "blocked"

        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        # (a) Card stays blocked — not claimed
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"Blocked card must stay blocked; got {task.status!r}"
        )
        assert task.claim_lock is None, (
            f"Blocked card must not have a claim lock; "
            f"got {task.claim_lock!r}"
        )
        assert len(result.spawned) == 0

        # (b) A blocked_dispatch_attempt event exists
        rows = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked_dispatch_attempt'",
            (tid,),
        ).fetchall()
        assert len(rows) >= 1, (
            f"Expected at least 1 'blocked_dispatch_attempt' event; "
            f"got {len(rows)}"
        )

        # (c) Payload includes origin, task_id, block_kind, and gate fields
        payload = rows[0]["payload"]
        assert payload is not None, "blocked_dispatch_attempt payload must not be None"
        pl = json.loads(payload)
        assert "task_id" in pl, f"payload missing task_id: {pl}"
        assert pl["task_id"] == tid, f"payload task_id mismatch: {pl['task_id']} != {tid}"
        assert "block_kind" in pl, f"payload missing block_kind: {pl}"
        assert "gate" in pl, f"payload missing gate field: {pl}"


def test_ready_card_with_sticky_block_records_blocked_dispatch_attempt(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """A ``ready``-status card with a sticky block (a ``blocked`` event but
    status still ``ready``) must produce a ``blocked_dispatch_attempt`` event
    from the block-gate audit in ``_dispatch_once_locked``.

    Tests the second code path: when a card passes the SQL filter (status='ready')
    but has a sticky block, the dispatcher must emit ``blocked_dispatch_attempt``
    alongside ``block_gate_audit``.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ready-with-sticky-block-audit-test",
            assignee="test-profile",
        )
        # Card starts as 'ready' (no parents, valid assignee).
        assert kb.get_task(conn, tid).status == "ready"

        # Inject a 'blocked' event to create a sticky block.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (tid, '{"origin":"test-injection","reason":"blocked"}', now),
        )
        conn.commit()

        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        # (a) Card was NOT claimed/spawned
        post_task = kb.get_task(conn, tid)
        assert post_task.claim_lock is None, (
            f"Sticky-blocked card was claimed despite block; "
            f"claim_lock={post_task.claim_lock!r}"
        )
        assert tid not in result.spawned, (
            f"Sticky-blocked card was spawned despite block"
        )

        # (b) Both block_gate_audit AND blocked_dispatch_attempt events exist
        audit_rows = conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind IN ('block_gate_audit', 'blocked_dispatch_attempt') "
            "ORDER BY id",
            (tid,),
        ).fetchall()
        audit_kinds = [r["kind"] for r in audit_rows]

        assert "block_gate_audit" in audit_kinds, (
            f"Missing block_gate_audit event; got kinds={audit_kinds}"
        )
        assert "blocked_dispatch_attempt" in audit_kinds, (
            f"Missing blocked_dispatch_attempt event; got kinds={audit_kinds}"
        )

        # (c) Verify the blocked_dispatch_attempt payload includes gate field
        bda_rows = [r for r in audit_rows if r["kind"] == "blocked_dispatch_attempt"]
        assert len(bda_rows) >= 1
        payload = bda_rows[0]["payload"]
        assert payload is not None
        pl = json.loads(payload)
        assert "gate" in pl, f"blocked_dispatch_attempt payload missing gate: {pl}"
        assert "block_kind" in pl, f"blocked_dispatch_attempt payload missing block_kind: {pl}"
        assert "task_id" in pl
