"""Regression tests for t_1ab401f2 / t_e5d5c8fc / t_179399dc: blocked-card dispatch guard.

These tests enforce that a task whose most recent ``blocked`` event lacks a
subsequent legitimate unblock event (``unblocked`` / ``promoted`` /
``promoted_manual``) must NOT be dispatched — even if its status is ``ready``
at the start of a dispatcher tick.

The soak audit (2026-08-05) found 5 blocked->claimed transitions where no
valid unblock event existed after the block.  The trigger was the
``nous-storm-recovery`` actuator doing a bare ``UPDATE tasks SET
status='ready'`` (no unblock event), and the enabler was the deployed dispatch
claim path lacking a sticky-block check.

Each scenario below reproduces one such violation as an invariant assertion on
the kernel-level code paths (claim_task + recompute_ready + dispatch_once).

Run:  pytest tests/hermes_cli/test_kanban_blocked_dispatch_guard.py -v
RED-FIRST: the *new* tests (recovery-actuator shapes) fail against unpatched
code and pass after the t_179399dc fix.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return h


@pytest.fixture
def all_assignees_spawnable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assume every assignee is a real Hermes profile so the dispatcher
    does not skip tasks as nonspawnable."""
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


def _set(conn: sqlite3.Connection, tid: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    conn.commit()


def _insert_raw_event(conn: sqlite3.Connection, tid: str,
                      kind: str, payload: dict | None) -> None:
    """Directly insert an event row with JSON-encoded payload."""
    pl = json.dumps(payload) if payload else None
    now = int(time.time())
    conn.execute(
        "INSERT INTO task_events(task_id,kind,payload,created_at) "
        "VALUES(?,?,?,?)",
        (tid, kind, pl, now),
    )
    conn.commit()


def _make_blocked_via_sql(conn: sqlite3.Connection, tid: str,
                          reason: str) -> None:
    """Model a worker-initiated block WITHOUT using block_task().

    This sets status='blocked' AND emits a ``blocked`` event, exactly
    what the dispatcher expects to see during a sticky-block scan.
    Using raw SQL avoids the expected_run_id CAS gate so we can create
    the exact race-condition states the soak violations exhibited.
    """
    _set(conn, tid, "blocked")
    _insert_raw_event(conn, tid, "blocked", {"reason": reason})


def _recovery_actuator_flip(conn: sqlite3.Connection, tid: str) -> None:
    """Model the nous-storm-recovery bare UPDATE (2026-08-05 trigger).

    ``UPDATE tasks SET status='ready', block_kind=NULL,
    consecutive_failures=0`` with NO unblock event. Also clear
    claim_lock so the row satisfies the dispatcher's ready-rows WHERE
    clause (status='ready' AND claim_lock IS NULL).
    """
    conn.execute(
        "UPDATE tasks SET status='ready', block_kind=NULL, "
        "consecutive_failures=0, claim_lock=NULL, claim_expires=NULL "
        "WHERE id=?",
        (tid,),
    )
    conn.commit()


def _spy_spawn():
    """Return a spawn_fn that records calls + returns a fake PID."""
    calls: list[str] = []

    def spy(task, workspace_path, board=None):
        calls.append(getattr(task, "id", str(task)))
        return 999999  # fake PID

    spy.calls = calls  # type: ignore[attr-defined]
    return spy


def _event_count(conn: sqlite3.Connection, task_id: str,
                 kind: str) -> int:
    rows = conn.execute(
        "SELECT COUNT(*) AS cnt FROM task_events "
        "WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()
    return rows["cnt"] if rows else 0


# -----------------------------------------------------------------------
# Case 1: blocked card dispatch MUST BE REJECTED
# -----------------------------------------------------------------------
def test_case1_blocked_card_dispatch_rejected(home: Path) -> None:
    """A task that calls ``block_task`` must stay blocked — never promoted
    by ``recompute_ready``, never claimable via ``claim_task``."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="case1-blocked", assignee="worker")
        kb.claim_task(conn, tid, claimer="worker")
        kb.block_task(
            conn, tid,
            reason="review-required: human decision needed",
            kind="capability",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        # Dispatcher ticks: must NOT promote.
        for _ in range(5):
            promoted = kb.recompute_ready(conn)
            assert promoted == 0, (
                "CASE 1 FAIL: recompute_ready promoted a sticky-blocked task"
            )
            assert kb.get_task(conn, tid).status == "blocked"

        # Direct claim must also refuse.
        claimed = kb.claim_task(conn, tid, claimer="dispatcher")
        assert claimed is None, (
            "CASE 1 FAIL: claim_task accepted a blocked task"
        )


# -----------------------------------------------------------------------
# Case 2: block → unblock → dispatch is ALLOWED
# -----------------------------------------------------------------------
def test_case2_block_then_unblock_then_dispatch_allowed(home: Path) -> None:
    """An explicit ``unblock_task`` clears the sticky state and the
    task returns to ready for normal dispatch."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="case2", assignee="worker")
        kb.claim_task(conn, tid, claimer="worker")
        kb.block_task(
            conn, tid,
            reason="review-required", kind="capability",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        assert kb.get_task(conn, tid).status == "blocked"

        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"

        # Should already be ready — nothing left to promote.
        assert kb.recompute_ready(conn) == 0
        claimed = kb.claim_task(conn, tid, claimer="dispatcher")
        assert claimed is not None, (
            "CASE 2 FAIL: unblocked-ready task must be claimable"
        )
        assert claimed.status == "running"


# -----------------------------------------------------------------------
# Case 3: unblock BEFORE block does NOT satisfy the requirement
# -----------------------------------------------------------------------
def test_case3_unblock_before_block_is_not_validating(home: Path) -> None:
    """An ``unblocked`` event that fires before the worker-initiated
    ``blocked`` event must NOT clear the sticky block.

    Hazard pattern: parent-done auto-unblock (``unblocked`` at T0), then
    the task gets blocked by the circuit breaker or manual action at T1 —
    but recompute_ready sees the stale unblocked and wrongly considers it
    cleared.  _has_sticky_block checks the MOST RECENT event; if that
    shows ``blocked`` (later than ``unblocked``) the task IS sticky.
    """
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="case3", assignee="worker")

        # Step A: Emit an early ``unblocked`` event to simulate parent
        # dependency clearance firing first.
        _set(conn, tid, "blocked")
        _insert_raw_event(conn, tid, "unblocked", {"source": "parent_done"})

        # Step B: Now someone flips status back to blocked and emits a
        # NEW ``blocked`` event (this is the "manual hold" or circuit-breaker
        # kick-in that comes LATER than the unblock).
        _insert_raw_event(conn, tid, "blocked",
                          {"reason": "review-required"})

        # Check: both audit-aligned and sticky predicates MUST return True.
        assert kb._has_sticky_block(conn, tid) is True, (
            "_has_sticky_block must return True when blocked event is latest"
        )
        assert kb._has_unresolved_block(conn, tid) is True, (
            "_has_unresolved_block must return True when blocked event is latest"
        )

        # Dispatcher tick: recompute_ready must NOT promote a sticky block.
        promoted = kb.recompute_ready(conn)
        assert promoted == 0, (
            f"CASE 3 FAIL: promoted={promoted}; unblock-before-block "
            "must not satisfy the guard"
        )
        assert kb.get_task(conn, tid).status == "blocked"


# -----------------------------------------------------------------------
# Case 4: out-of-order / concurrent block during dispatch fails closed
# -----------------------------------------------------------------------
def test_case4_out_of_order_block_during_dispatch_fails_closed(home: Path) -> None:
    """If a worker calls ``block_task`` while the dispatcher loop has
    already queued the task for spawn, the block must win — the task
    must remain blocked through recompute_ready and claim_task must
    refuse even when the status column reads 'ready'."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="case4", assignee="worker")
        kb.claim_task(conn, tid, claimer="worker")

        # Simulate external code flipping status to ready while emitting
        # a blocked event (e.g., a cron unblock followed immediately by
        # a block from another thread). We bypass block_task CAS to model
        # the race.
        _set(conn, tid, "ready")
        _insert_raw_event(conn, tid, "blocked",
                          {"reason": "concurrent-block-during-dispatch"})
        # Clear the claim_lock so the row satisfies the ready-rows WHERE
        # clause — this is the state the recovery actuator produces.
        conn.execute(
            "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL "
            "WHERE id=?", (tid,),
        )
        conn.commit()

        # claim_task must reject a ready-status card carrying an orphaned
        # blocked event with no intervening unblock (t_179399dc fix).
        claimed = kb.claim_task(conn, tid, claimer="dispatcher")
        assert claimed is None, (
            "CASE 4 GAP: claim_task accepted a task with an orphaned "
            "blocked event but no corresponding unblock event"
        )


# -----------------------------------------------------------------------
# NEW (t_179399dc): recovery-actuator direct-set-ready + dispatch
# -----------------------------------------------------------------------
def test_recovery_actuator_direct_set_ready_not_dispatched(
    home: Path,
    all_assignees_spawnable: None,
) -> None:
    """THE regression shape from the root-cause report (§6.5): simulate the
    recovery actuator (bare UPDATE status='ready' on a sticky-blocked row,
    no unblock event) and assert dispatch_once does NOT claim it."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="soak-recovery-trigger", assignee="worker",
        )
        # Worker-initiated block (real gate), then the actuator flips the
        # status column directly with NO unblock event.
        _make_blocked_via_sql(conn, tid, "review-required")
        assert kb._has_unresolved_block(conn, tid) is True
        _recovery_actuator_flip(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"

        # Full dispatch tick must refuse: no spawn, audit event recorded.
        spy = _spy_spawn()
        result = kb.dispatch_once(conn, spawn_fn=spy)

        assert len(spy.calls) == 0, (
            f"RECOVERY-TRIGGER FAIL: spawn_fn called for sticky-blocked "
            f"card (calls={spy.calls!r})"
        )
        assert tid not in result.spawned, (
            f"RECOVERY-TRIGGER FAIL: ready+sticky-blocked card was spawned; "
            f"spawned={result.spawned!r}"
        )
        assert tid in result.blocked_claim_attempts, (
            "RECOVERY-TRIGGER FAIL: card must be in blocked_claim_attempts; "
            f"got {result.blocked_claim_attempts!r}"
        )
        assert _event_count(conn, tid, "blocked_dispatch_attempt") >= 1, (
            "RECOVERY-TRIGGER FAIL: no blocked_dispatch_attempt audit event"
        )
        # Card stays ready (the gate is a no-mutate refusal, not a status
        # change) — but it must never be claimed.
        assert kb.get_task(conn, tid).status == "ready"


def test_recovery_actuator_claim_task_refuses(
    home: Path,
) -> None:
    """Direct claim_task on a recovery-actuator-flipped card must refuse
    (fail closed at the claim primitive, even if the dispatch gate were
    bypassed)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="soak-recovery-claim", assignee="worker")
        _make_blocked_via_sql(conn, tid, "frank-gated")
        _recovery_actuator_flip(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"

        claimed = kb.claim_task(conn, tid, claimer="dispatcher")
        assert claimed is None, (
            "CLAIM-GUARD FAIL: claim_task accepted a recovery-flipped "
            "sticky-blocked card"
        )
        # The refusal must be observable via the claim_rejected event.
        assert _event_count(conn, tid, "claim_rejected") >= 1


def test_unblocked_after_block_claimable_after_fix(
    home: Path,
) -> None:
    """After an explicit unblocked event, the card is claimable again —
    the fail-closed guard must NOT over-block the sanctioned exit."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="unblock-exit", assignee="worker")
        _make_blocked_via_sql(conn, tid, "review-required")
        kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"

        claimed = kb.claim_task(conn, tid, claimer="dispatcher")
        assert claimed is not None, (
            "UNBLOCK-EXIT FAIL: unblocked card must be claimable"
        )


def test_promoted_manual_after_block_claimable(
    home: Path,
) -> None:
    """Operator ``promote_task`` (emits ``promoted_manual``) is a legitimate
    unblock per the soak monitor's UNBLOCK_KINDS — the claim guard must not
    strand it (audit-aligned predicate)."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="promote-exit", assignee="worker")
        _make_blocked_via_sql(conn, tid, "review-required")
        ok, err = kb.promote_task(conn, tid, actor="operator", force=True)
        assert ok, f"promote_task failed: {err}"
        assert kb.get_task(conn, tid).status == "ready"
        assert kb._has_unresolved_block(conn, tid) is False, (
            "promoted_manual must clear the unresolved-block predicate"
        )

        claimed = kb.claim_task(conn, tid, claimer="dispatcher")
        assert claimed is not None, (
            "PROMOTE-EXIT FAIL: operator-promoted card must be claimable"
        )


# -----------------------------------------------------------------------
# Scenario 1: t_67d438b9 — LOCAL RUNG: finish gpt-oss-120b download
# -----------------------------------------------------------------------
def test_soak_violation_scenario_1_t_67d438b9(home: Path) -> None:
    """Event trace: ``gave_up`` at T−7000, ``blocked`` at T−500. No
    ``unblocked`` ever. Task ended up ``running`` despite being blocked."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="LOCAL RUNG: finish gpt-oss-120b download",
            assignee="local-worker",
        )

        _insert_raw_event(conn, tid, "gave_up", {"error": "timeout"})
        _make_blocked_via_sql(conn, tid, "manual hold")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0, (
            "SCENARIO 1 FAIL: gave_up+blocked task promoted without unblock"
        )
        assert kb.get_task(conn, tid).status == "blocked"


# -----------------------------------------------------------------------
# Scenario 2: t_710bd212 — Re-verify 6 failing cron jobs
# -----------------------------------------------------------------------
def test_soak_violation_scenario_2_t_710bd212(home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="Re-verify 6 failing cron jobs AFTER signal_journeys P0",
            assignee="cron-worker",
        )
        _make_blocked_via_sql(conn, tid, "waiting for signal_journeys")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0, (
            "SCENARIO 2 FAIL: cron-task promoted without unblock"
        )
        assert kb.get_task(conn, tid).status == "blocked"


# -----------------------------------------------------------------------
# Scenario 3: t_bff86f7b — VOICE-2 ConversationRelay end-of-call cutoff
# -----------------------------------------------------------------------
def test_soak_violation_scenario_3_t_bff86f7b(home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="VOICE-2 (grok-safe): fix ConversationRelay "
                  "end-of-call cutoff",
            assignee="voice-agent",
        )
        _make_blocked_via_sql(conn, tid, "voice-broker phase 0 gate")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0, (
            "SCENARIO 3 FAIL: voice-task promoted without unblock"
        )


# -----------------------------------------------------------------------
# Scenario 4: t_d23b5adc — VOICE-BROKER Phase 0 Gate
# -----------------------------------------------------------------------
def test_soak_violation_scenario_4_t_d23b5adc(home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="VOICE-BROKER Phase 0 Gate: Security/Compliance Checklist",
            assignee="voice-broker",
        )
        _make_blocked_via_sql(conn, tid, "security-gate-not-met")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0, (
            "SCENARIO 4 FAIL: voice-broker-task promoted without unblock"
        )


# -----------------------------------------------------------------------
# Scenario 5: t_e78930fe — FRANK-GATED whatsapp alert channel dead
# -----------------------------------------------------------------------
def test_soak_violation_scenario_5_t_e78930fe(home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn,
            title="FRANK-GATED: whatsapp:Frank alert channel dead 10 days",
            assignee="gateway-agent",
        )
        _make_blocked_via_sql(conn, tid, "frank-gated: human approval needed")

        promoted = kb.recompute_ready(conn)
        assert promoted == 0, (
            "SCENARIO 5 FAIL: frank-gated task promoted without unblock"
        )


# -----------------------------------------------------------------------
# Guard anchor: circuit-breaker blocks SHOULD still auto-recover
# -----------------------------------------------------------------------
def test_circuit_breaker_blocks_still_auto_recover(home: Path) -> None:
    """Circuit-breaker blocks (only ``gave_up`` event, NO ``blocked``
    event) must auto-recover when parents finish.  This preserves the
    original intent of #40c1decb3.

    Key distinction: ``_has_sticky_block`` returns False when there's no
    ``blocked`` event at all — only ``gave_up``.  So the task should
    flow through recompute_ready normally.
    """
    with kb.connect_closing() as conn:
        # Parent completes first.
        pid = kb.create_task(conn, title="parent-task", assignee="worker")
        _set(conn, pid, "done")

        # Child depends on parent, starts blocked. Use a real profile name.
        child_tid = kb.create_task(
            conn, title="crashed job", assignee="retry-worker",
        )

        kb.link_tasks(conn, parent_id=pid, child_id=child_tid)

        # Set status to blocked manually. Only give_up event — no blocked
        # event. Not sticky.
        _set(conn, child_tid, "blocked")
        _insert_raw_event(conn, child_tid, "gave_up", {"failures": 3})

        # With parent done AND no sticky block, recompute_ready must promote.
        promoted = kb.recompute_ready(conn)
        assert promoted >= 1, (
            "CB REGRESSION: circuit-breaker block must auto-recover"
        )
        assert kb.get_task(conn, child_tid).status == "ready", (
            "CB REGRESSION: child should be ready after parent done"
        )
