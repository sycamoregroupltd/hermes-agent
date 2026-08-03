"""Regression tests for t_e6bb0f1e — every transition out of ``blocked``
must emit the block-lifecycle ``unblocked`` event BEFORE persisting
``ready``, so the dispatcher's ``_has_sticky_block()`` gate flips off.

Root cause: the governor/PM promote path (``promote_task``) and the
PUSH-PASS writers (``reclaim_task``, dashboard drag-drop
``_set_status_direct``, the fleet silent-exit reaper) flipped a card from
``blocked`` to ``ready`` without emitting ``unblocked``.  A card written
that way sits in ``ready`` forever: ``_has_sticky_block()`` still sees the
pending ``blocked`` event and the dispatcher refuses to claim it.

Fix: a shared writer-layer helper ``_emit_unblocked`` (plus
``_latest_block_ref``) that every blocked→ready writer must call.  It is
idempotent (no event when no ``blocked`` event is pending) and attaches
``block_ref`` / ``block_reason`` / ``block_kind`` from the original block.

These tests pin the contract:

* ``promote_task`` from ``blocked`` emits ``unblocked`` with a block_ref
  and clears the sticky gate.
* ``promote_task`` from ``todo`` emits no ``unblocked`` (nothing pending).
* ``unblock_task`` emits ``unblocked`` with a block_ref on both the
  ``ready`` and ``todo`` landing paths.
* ``reclaim_task`` from ``blocked`` emits ``unblocked`` with a block_ref.
* ``_emit_unblocked`` is idempotent and a no-op without a pending block.
* After a blocked→ready promote, the dispatcher actually claims + spawns
  the card (the end-to-end symptom of the bug).
"""

from __future__ import annotations

import json
import secrets
import signal
import time
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


def _blocked_event_id(conn, task_id: str) -> int:
    """Return the id of the most recent ``blocked`` event for ``task_id``."""
    row = conn.execute(
        "SELECT id FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None, "no blocked event present"
    return int(row["id"])


def _unblocked_events(conn, task_id: str) -> list:
    """Return the ``unblocked`` event rows (parsed payloads) for ``task_id``."""
    rows = conn.execute(
        "SELECT id, payload FROM task_events "
        "WHERE task_id = ? AND kind = 'unblocked' "
        "ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        payload = None
        if r["payload"]:
            try:
                payload = json.loads(r["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = None
        out.append({"id": int(r["id"]), "payload": payload})
    return out


# ---------------------------------------------------------------------------
# promote_task: the governor/PM promote call site
# ---------------------------------------------------------------------------


def test_promote_blocked_task_emits_unblocked_with_block_ref(kanban_home: Path) -> None:
    """promote_task from ``blocked`` must emit ``unblocked`` carrying the
    original block's event id (block_ref) BEFORE ``promoted_manual``, and
    must clear the dispatcher's sticky-block gate."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="blocked", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input") is True
        blocked_ref = _blocked_event_id(conn, t)
        assert kb.get_task(conn, t).status == "blocked"
        assert kb._has_sticky_block(conn, t) is True

        ok, err = kb.promote_task(conn, t, actor="ops", reason="forced clear")
        assert ok is True
        assert err is None
        assert kb.get_task(conn, t).status == "ready"
        assert kb._has_sticky_block(conn, t) is False

        unblocked = _unblocked_events(conn, t)
        assert len(unblocked) == 1
        payload = unblocked[0]["payload"] or {}
        assert payload.get("block_ref") == blocked_ref, (
            f"unblocked must reference the original blocked event; "
            f"got {payload.get('block_ref')!r} want {blocked_ref}"
        )
        assert payload.get("reason") == "promote"
        assert payload.get("actor") == "ops"
        assert payload.get("block_reason") == "need input"
        # Ordering: unblocked before promoted_manual.
        kinds = [e.kind for e in kb.list_events(conn, t)]
        assert kinds.index("unblocked") < kinds.index("promoted_manual")


def test_promote_todo_task_does_not_emit_unblocked(kanban_home: Path) -> None:
    """promote_task from ``todo`` is a pure promotion — no block is pending,
    so no ``unblocked`` event may be emitted."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="todo card", assignee="a")
        # New cards with no parents land in 'todo' when created via
        # kanban_create with parents; here 'ready' is the default, so park
        # it in todo first.
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (t,))
        conn.commit()
        assert kb.get_task(conn, t).status == "todo"

        ok, err = kb.promote_task(conn, t, actor="ops")
        assert ok is True
        assert err is None
        assert kb.get_task(conn, t).status == "ready"
        assert _unblocked_events(conn, t) == []
        assert kb._has_sticky_block(conn, t) is False


# ---------------------------------------------------------------------------
# unblock_task: the kanban_unblock / CLI call site
# ---------------------------------------------------------------------------


def test_unblock_task_emits_unblocked_with_block_ref(kanban_home: Path) -> None:
    """unblock_task from ``blocked`` must emit ``unblocked`` carrying the
    original block reference and clear the sticky gate."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="blocked", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(
            conn, t, reason="review-required: sign-off", kind="needs_input",
        ) is True
        blocked_ref = _blocked_event_id(conn, t)

        assert kb.unblock_task(conn, t) is True
        assert kb.get_task(conn, t).status == "ready"
        assert kb._has_sticky_block(conn, t) is False

        unblocked = _unblocked_events(conn, t)
        assert len(unblocked) == 1
        payload = unblocked[0]["payload"] or {}
        assert payload.get("block_ref") == blocked_ref
        assert payload.get("block_reason") == "review-required: sign-off"
        assert payload.get("block_kind") == "needs_input"


def test_unblock_task_with_pending_parents_emits_unblocked_on_todo_path(
    kanban_home: Path,
) -> None:
    """When parents are still open, unblock_task lands in ``todo`` — it must
    STILL emit ``unblocked`` (the block is resolved even though the card
    waits on dependencies before it can be claimed)."""
    with kb.connect() as conn:
        # Create the child first (parent-free so claim -> running), drive it
        # into 'blocked' via a worker block, then add the open parent so the
        # subsequent unblock_task must land in 'todo' (not 'ready').
        child = kb.create_task(conn, title="child", assignee="a")
        kb.claim_task(conn, child)
        assert kb.get_task(conn, child).status == "running"
        assert kb.block_task(
            conn, child, reason="waiting on gate", kind="needs_input",
            expected_run_id=kb.get_task(conn, child).current_run_id,
        ) is True
        assert kb.get_task(conn, child).status == "blocked"
        blocked_ref = _blocked_event_id(conn, child)
        parent = kb.create_task(conn, title="parent", assignee="a")
        kb.link_tasks(conn, parent, child)  # now parent is open

        assert kb.unblock_task(conn, child) is True
        assert kb.get_task(conn, child).status == "todo"  # parent not done
        assert kb._has_sticky_block(conn, child) is False

        unblocked = _unblocked_events(conn, child)
        assert len(unblocked) == 1
        payload = unblocked[0]["payload"] or {}
        assert payload.get("block_ref") == blocked_ref
        assert payload.get("status") == "todo"


# ---------------------------------------------------------------------------
# reclaim_task: operator-driven reclaim of a blocked card
# ---------------------------------------------------------------------------


def test_reclaim_blocked_task_emits_unblocked_with_block_ref(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reclaim_task from ``blocked`` must emit ``unblocked`` with a
    block_ref so the sticky gate clears."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="blocked", assignee="a")
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="need input") is True
        assert kb.get_task(conn, t).status == "blocked"
        assert kb._has_sticky_block(conn, t) is True
        blocked_ref = _blocked_event_id(conn, t)

        # reclaim_task requires a claim_lock on the row (it releases it).
        lock = f"{kb._claimer_id().split(':', 1)[0]}:{secrets.token_hex(8)}"
        future = int(time.time()) + 3600
        state = {"alive": True}

        def _signal(_pid, sig):
            if sig == signal.SIGTERM:
                state["alive"] = False

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: state["alive"])
        conn.execute(
            "UPDATE tasks SET claim_lock=?, claim_expires=?, worker_pid=? "
            "WHERE id=?",
            (lock, future, 12345, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'blocked', ?, ?, ?, ?)",
            (t, lock, future, 12345, int(time.time())),
        )
        conn.commit()

        assert kb.reclaim_task(conn, t, reason="operator reset", signal_fn=_signal) is True
        assert kb.get_task(conn, t).status == "ready"
        assert kb._has_sticky_block(conn, t) is False

        unblocked = _unblocked_events(conn, t)
        assert len(unblocked) == 1
        payload = unblocked[0]["payload"] or {}
        assert payload.get("block_ref") == blocked_ref
        assert payload.get("reason") == "reclaim"
        assert payload.get("prev_status") == "blocked"
        kinds = [e.kind for e in kb.list_events(conn, t)]
        assert kinds.index("unblocked") < kinds.index("reclaimed")


# ---------------------------------------------------------------------------
# _emit_unblocked helper: idempotence + no-op semantics
# ---------------------------------------------------------------------------


def test_emit_unblocked_is_idempotent(kanban_home: Path) -> None:
    """The shared helper must emit exactly once per pending block: a second
    call after the event fired is a no-op (returns False, adds no row)."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="blocked", assignee="a")
        kb.claim_task(conn, t)
        kb.block_task(conn, t, reason="need input")
        blocked_ref = _blocked_event_id(conn, t)

        assert kb._emit_unblocked(conn, t, reason="test") is True
        assert len(_unblocked_events(conn, t)) == 1
        assert kb._has_sticky_block(conn, t) is False

        # Second call: latest block-lifecycle event is now 'unblocked' — no-op.
        assert kb._emit_unblocked(conn, t, reason="test") is False
        assert len(_unblocked_events(conn, t)) == 1

        # A fresh re-block creates a fresh pending block → new unblocked.
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, ?)",
            (t, json.dumps({"reason": "re-block"}), int(time.time())),
        )
        conn.commit()
        assert kb._emit_unblocked(conn, t, reason="test2") is True
        unblocked = _unblocked_events(conn, t)
        assert len(unblocked) == 2
        assert (unblocked[-1]["payload"] or {}).get("block_ref") != blocked_ref


def test_emit_unblocked_noop_without_pending_block(kanban_home: Path) -> None:
    """Circuit-breaker blocks (``gave_up`` event, no ``blocked`` event) are
    intentionally non-sticky — the helper must NOT emit a spurious
    ``unblocked`` for them."""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="breaker", assignee="a")
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (t,))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'gave_up', NULL, ?)",
            (t, int(time.time())),
        )
        conn.commit()

        assert kb._has_sticky_block(conn, t) is False
        assert kb._emit_unblocked(conn, t, reason="test") is False
        assert _unblocked_events(conn, t) == []


# ---------------------------------------------------------------------------
# End-to-end: the dispatcher claims a card after blocked→ready promote
# ---------------------------------------------------------------------------


def test_dispatch_claims_promoted_blocked_card(
    kanban_home: Path, all_assignees_spawnable,
) -> None:
    """The bug symptom: a blocked→ready card WITHOUT an unblocked event is
    refused by the dispatcher forever (``_has_sticky_block`` stays true).
    After promote_task emits ``unblocked``, dispatch_once must claim and
    spawn the card."""
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="recoverable", assignee="test-profile",
        )
        kb.claim_task(conn, t)
        assert kb.block_task(conn, t, reason="review-required: temp park") is True
        assert kb.get_task(conn, t).status == "blocked"

        # Governor/PM push-pass: promote the blocked card to ready.
        ok, err = kb.promote_task(conn, t, actor="governor", reason="push-pass")
        assert ok is True, err
        assert kb.get_task(conn, t).status == "ready"
        assert kb._has_sticky_block(conn, t) is False

        spawn_calls: list[str] = []

        def spy_spawn(task, workspace_path, board=None):
            spawn_calls.append(getattr(task, "id", str(task)))
            return 999999  # fake PID

        result = kb.dispatch_once(conn, spawn_fn=spy_spawn)
        spawned_ids = [sid for (sid, _a, _w) in result.spawned]
        assert t in spawned_ids, (
            f"promoted blocked card must be dispatched once unblocked; "
            f"spawned={result.spawned!r}"
        )
        assert spawn_calls == [t]
        assert kb.get_task(conn, t).status == "running"
