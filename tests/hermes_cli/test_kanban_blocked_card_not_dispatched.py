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


# ── status-first gate: stale blocked tail vs genuinely blocked (t_d1e107a2) ──


def test_ready_card_with_stale_blocked_event_tail_is_spawnable(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """t_d1e107a2 regression: a card whose CURRENT status is ``ready`` with
    only a stale ``blocked`` event tail (the unblock event was missed — a
    force-promote, historical card, or any path that dropped it) MUST be
    spawnable.

    Before the status-first gate, ``_has_sticky_block`` read only the last
    block-lifecycle event, so a ready card whose last event was ``blocked``
    was refused forever — the 07-30 fleet stall refused 22,044 dispatches
    this way. The current lifecycle state (status='ready') is a CLEAR
    transition out of blocked and wins over the event tail.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="ready-with-stale-blocked-tail",
            assignee="test-profile",
        )
        # Task is ready (no parents, valid assignee).
        assert kb.get_task(conn, tid).status == "ready"

        # Inject a stale 'blocked' event WITHOUT touching the status column
        # (simulates a force-promote / missed-unblocked historical card).
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (tid, '{"origin":"test-injection","reason":"stale"}', now),
        )
        conn.commit()

        # Status-first gate: a ready card is NOT sticky even with a stale
        # 'blocked' event tail.
        assert kb._has_sticky_block(conn, tid) is False, (
            "ready card with stale blocked tail must not be sticky"
        )

        # Dispatch MUST claim and spawn it.
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        assert tid in spy.calls, (
            f"ready card with stale blocked tail must be spawned; "
            f"got spawned={result.spawned!r}"
        )
        assert len(spy.calls) == 1, (
            f"spawn_fn must be called exactly once for the ready card; "
            f"got calls={spy.calls!r}"
        )
        assert len(result.spawned) == 1, (
            f"DispatchResult.spawned must list the ready card; "
            f"got {result.spawned!r}"
        )
        # No audit: the card is not blocked, so it must not land in any
        # blocked-card telemetry bucket.
        assert tid not in result.skipped_block_gate
        assert tid not in result.blocked_claim_attempts
        assert _block_gate_audit_count(conn, tid) == 0, (
            "no block_gate_audit event expected for a ready card"
        )


def test_genuinely_blocked_card_stays_gated(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """t_d1e107a2 regression: a GENUINELY blocked card — current status
    column is 'blocked' (worker/operator ``kanban_block``) — must stay
    gated even if an earlier event tail could be misread. The status-first
    gate preserves approval blocks: status='blocked' wins over everything.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="genuinely-blocked",
            assignee="test-profile",
            initial_status="running",
        )
        assert kb.get_task(conn, tid).status == "ready"

        # A real worker/operator block: status flips to 'blocked' AND a
        # 'blocked' event is emitted (block_task does both).
        kb.block_task(conn, tid, reason="review-required: human eyes please")
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"

        # Status-first gate: blocked status is always sticky.
        assert kb._has_sticky_block(conn, tid) is True, (
            "genuinely blocked card must be sticky"
        )

        # Dispatch must NOT claim or spawn it, and must keep it blocked.
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", (
            f"genuinely blocked card must stay blocked; got {task.status!r}"
        )
        assert tid not in [s[0] for s in result.spawned]
        assert len(spy.calls) == 0, (
            f"spawn_fn must not be called for a blocked card; "
            f"got calls={spy.calls!r}"
        )
        assert task.claim_lock is None, (
            f"blocked card must not be claimed; claim_lock={task.claim_lock!r}"
        )


def test_ready_card_stale_tail_not_in_skipped_bucket(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """t_d1e107a2: defense-in-depth audit must NOT fire for a ready card
    that merely carries a stale 'blocked' event tail. The
    ``skipped_block_gate`` / ``block_gate_audit`` machinery exists for
    cards whose CURRENT lifecycle state is blocked — a ready card is a
    clear transition out of blocked and is spawned, not skipped.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="stale-tail-no-audit",
            assignee="test-profile",
        )
        assert kb.get_task(conn, tid).status == "ready"

        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (tid, '{"origin":"test","reason":"stale"}', now),
        )
        conn.commit()

        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        assert tid in spy.calls, (
            f"ready card with stale tail must be spawned; got spawned={result.spawned!r}"
        )
        assert len(result.spawned) == 1, f"got {result.spawned!r}"
        assert tid not in result.skipped_block_gate
        assert tid not in result.blocked_claim_attempts
        assert _block_gate_audit_count(conn, tid) == 0
        assert len(_blocked_dispatch_attempt_events(conn, tid)) == 0


# ── t_73a70cde: blocked-card exclusion + audit logging ────────────────────


def _blocked_dispatch_attempt_events(conn, task_id: str) -> list[dict]:
    """Return parsed ``blocked_dispatch_attempt`` events for ``task_id``."""
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked_dispatch_attempt'",
        (task_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]) if r["payload"] else {})
        except (json.JSONDecodeError, TypeError):
            out.append({})
    return out


def test_t73a70cde_sticky_block_audit_event_metadata(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """t_73a70cde acceptance (b): every blocked-card claim attempt logs a
    ``blocked_dispatch_attempt`` event carrying card id, timestamp, and
    dispatcher id.

    Under the status-first gate (t_d1e107a2), a READY card with a stale
    ``blocked`` event tail is spawnable — it is not a blocked card. The
    audit path fires for the racy-writer case the code comment describes:
    a card selected as ``ready`` in the snapshot that a concurrent writer
    flips to ``status='blocked'`` before the per-row re-read. We simulate
    that deterministically: card A's spawn flips card B to ``blocked``
    mid-loop, so when the loop reaches B the fresh re-read sees a blocked
    card and the audit fires.
    """
    with kb.connect() as conn:
        # Card A is processed first (earlier created_at); its spawn_fn
        # simulates the concurrent operator block on card B.
        a = kb.create_task(conn, title="t73-a", assignee="test-profile")
        b = kb.create_task(conn, title="t73-b", assignee="test-profile")
        assert kb.get_task(conn, a).status == "ready"
        assert kb.get_task(conn, b).status == "ready"

        flips: list[str] = []

        def spy(task, workspace_path, board=None):
            # Simulate a concurrent writer blocking card B while A is being
            # claimed/spawned — the exact racy re-read case.
            conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (b,))
            conn.commit()
            flips.append(getattr(task, "id", str(task)))
            return 999999

        result = kb.dispatch_once(conn, spawn_fn=spy)

        # (a) A spawned; B was never claimed (blocked mid-loop).
        assert a in [s[0] for s in result.spawned]
        assert flips == [a]
        assert b not in [s[0] for s in result.spawned]
        assert kb.get_task(conn, b).status == "blocked"
        # (b) audit event present with required metadata for B
        assert b in result.blocked_claim_attempts
        assert b in result.skipped_block_gate
        events = _blocked_dispatch_attempt_events(conn, b)
        assert events, "blocked_dispatch_attempt event was not created"
        ev = events[0]
        assert ev.get("task_id") == b
        assert isinstance(ev.get("timestamp"), int) and ev["timestamp"] > 0
        assert ev.get("dispatcher_id")  # host:pid identifier
        assert ev.get("sticky_block") is True
        assert ev.get("status_column") == "blocked"


def test_t73a70cde_stale_tail_ready_card_is_not_an_audit_event(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """t_d1e107a2: the audit path must NOT fire for a ready card with a
    stale ``blocked`` event tail — that card is spawnable, so no
    ``blocked_dispatch_attempt`` is written. The audit is reserved for
    cards whose CURRENT lifecycle state is blocked (racy re-read or a
    genuinely blocked card reaching the claim loop).
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t73-stale-tail", assignee="test-profile",
        )
        assert kb.get_task(conn, tid).status == "ready"
        # Stale blocked event tail only — status stays 'ready'.
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (tid, '{"origin":"test","reason":"parked"}', now),
        )
        conn.commit()
        assert kb._has_sticky_block(conn, tid) is False

        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        assert tid in spy.calls, (
            f"ready card with stale tail must be spawned; got spawned={result.spawned!r}"
        )
        assert len(result.spawned) == 1, f"got {result.spawned!r}"
        assert tid not in result.skipped_block_gate
        assert tid not in result.blocked_claim_attempts
        assert _blocked_dispatch_attempt_events(conn, tid) == []
        assert _block_gate_audit_count(conn, tid) == 0


def test_t73a70cde_blind_spot_status_blocked_excluded_from_claim(
    kanban_home: Path,
    all_assignees_spawnable: None,
) -> None:
    """t_73a70cde acceptance (a): a card carrying ``status='blocked'`` with
    NO blocked event (the recompute_ready blind-spot safety net) must never
    be claimed/running.

    Mechanism: ``dispatch_once`` only selects ``WHERE status='ready'``, and
    ``recompute_ready``'s blind-spot guard refuses to promote
    ``status='blocked'`` rows, so such a card can never reach the claim loop.
    We assert it is absent from every spawned/audit bucket and stays blocked.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t73-blindspot", assignee="test-profile",
        )
        assert kb.get_task(conn, tid).status == "ready"
        # Blind-spot: flip status to blocked WITHOUT a blocked event.
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        conn.commit()

        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)
        # (a) never claim a blocked card
        assert tid not in [s[0] for s in result.spawned]
        assert len(spy.calls) == 0
        # The card carries no blocked event, so the loop's sticky check does
        # not fire and no audit event is expected for this path — the
        # upstream blind-spot guard is the control.
        assert tid not in result.blocked_claim_attempts
        assert tid not in result.skipped_block_gate
        # Card remains blocked (not promoted to ready, not claimed).
        assert kb.get_task(conn, tid).status == "blocked"
