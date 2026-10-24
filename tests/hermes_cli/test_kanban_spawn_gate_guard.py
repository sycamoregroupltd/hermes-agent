"""Spawn-gate respawn guard: closes the same-tick promoted -> claimed race (jarvis-os/t_2311edb3).

The fleet overlay ``dispatcher_antithrash.py`` (a cron, outside the dispatcher) parks cards whose newest
block/wait/schedule event carries the structured ``SPAWN_GATE_PARK`` tag while the swap-hot latch is CLOSED.
It cannot beat the dispatcher: ``recompute_ready`` promotes a card and the SAME ``dispatch_once`` tick claims
and spawns it (t_69b954bb: promoted -> claimed 2 s apart). These tests replay that sequence through one real
``dispatch_once`` call and require the guard to hold the card inside the tick, with no outside timing.

Council conditions (2026-10-17): key on the latch, never blocker text (1); apply only to tagged cards and lift
when the latch clears (2). The guard is off unless ``kanban.spawn_gate_flag`` / HERMES_KANBAN_SPAWN_GATE_FLAG
names the latch file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

TAG_REASON = "SPAWN_GATE_PARK resume-at: 2026-10-18T10:55:00+00:00 gate CLOSED (swap-hot latch)"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_SPAWN_GATE_FLAG", raising=False)
    kb.init_db()
    return home


@pytest.fixture
def latch(tmp_path, monkeypatch):
    path = tmp_path / "swap-hot.flag"

    def set_state(state):
        if state is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(state + " since=2026-10-18T04:55:00Z\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_SPAWN_GATE_FLAG", str(path))
    return set_state


def _spawner(spawns):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 4242
    return fake_spawn


def _card_waiting_on_parent(conn, reason, *, promote_in_tick=False, monkeypatch=None):
    """The real t_69b954bb shape: a worker ran, found its upstream unfinished and blocked kind=dependency, so
    the card sits in todo with a dependency_wait event. Built through the public mutators, not raw SQL.

    Then the parent finishes. By default ``complete_task`` promotes the child at once (its own
    ``recompute_ready``) and the next tick claims it. With ``promote_in_tick`` that promotion is deferred, so
    the dispatcher's own ``recompute_ready`` promotes the child in the SAME tick that claims it."""
    parent = kb.create_task(conn, title="upstream", assignee="bob")
    child = kb.create_task(conn, title="gate-bound child", assignee="alice")
    claimed = kb.claim_task(conn, child)
    assert claimed is not None
    # The owning worker links its own active run, then dependency-blocks (the supported handoff path).
    kb.link_tasks(conn, parent, child, expected_child_run_id=claimed.current_run_id)
    assert kb.block_task(conn, child, reason=reason, kind="dependency")
    assert kb.get_task(conn, child).status == "todo"
    if promote_in_tick:
        with monkeypatch.context() as m:
            m.setattr(kb, "recompute_ready", lambda *a, **k: 0)
            assert kb.complete_task(conn, parent, result="ok", force=True)
        assert kb.get_task(conn, child).status == "todo"
    else:
        assert kb.complete_task(conn, parent, result="ok", force=True)
        assert kb.get_task(conn, child).status == "ready"
    return child


def _kinds(conn, task_id):
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,))]


@pytest.mark.parametrize("promote_in_tick", [True, False], ids=["promote-in-tick", "promote-at-parent-complete"])
def test_same_tick_promote_then_claim_is_held_while_latch_closed(
        kanban_home, latch, all_assignees_spawnable, monkeypatch, promote_in_tick):
    """Reproducer: before the guard the tick that promoted (or first saw) the tagged card also claimed and
    spawned it under a CLOSED latch. No cron overlay can act between those two steps."""
    latch("SWAP_HOT=1")
    spawns: list = []
    with kbc.connect() as conn:
        child = _card_waiting_on_parent(conn, TAG_REASON, promote_in_tick=promote_in_tick, monkeypatch=monkeypatch)
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
        assert res.promoted == (1 if promote_in_tick else 0)
        assert child not in spawns
        assert (child, "spawn_gate_closed") in res.respawn_guarded
        assert kb.get_task(conn, child).status == "ready"
        kinds = _kinds(conn, child)
        assert kinds[-2:] == ["promoted", "respawn_guarded"]
        assert "claimed" not in kinds[kinds.index("promoted"):]


def test_guard_lifts_when_latch_opens(kanban_home, latch, all_assignees_spawnable):
    latch("SWAP_HOT=1")
    spawns: list = []
    with kbc.connect() as conn:
        child = _card_waiting_on_parent(conn, TAG_REASON)
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
        assert spawns == []
        # Repeated ticks do not write one respawn_guarded row per tick.
        assert _kinds(conn, child).count("respawn_guarded") == 1
        latch("SWAP_HOT=0")
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
        assert spawns == [child]


def test_missing_latch_file_means_open(kanban_home, latch, all_assignees_spawnable):
    latch(None)
    spawns: list = []
    with kbc.connect() as conn:
        child = _card_waiting_on_parent(conn, TAG_REASON)
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [child]


def test_untagged_card_is_untouched_under_closed_latch(kanban_home, latch, all_assignees_spawnable):
    """Condition 1: blocker TEXT about the gate is not the tag; only the first-token tag binds a card."""
    latch("SWAP_HOT=1")
    spawns: list = []
    with kbc.connect() as conn:
        child = _card_waiting_on_parent(conn, "spawn/memory gate closed; waiting. SPAWN_GATE_PARK later")
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [child]


def test_flag_unset_keeps_existing_behaviour(kanban_home, tmp_path, monkeypatch, all_assignees_spawnable):
    """Feature flag defaults off: a tagged card under a CLOSED latch file still dispatches."""
    (tmp_path / "swap-hot.flag").write_text("SWAP_HOT=1\n", encoding="utf-8")
    spawns: list = []
    with kbc.connect() as conn:
        child = _card_waiting_on_parent(conn, TAG_REASON)
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [child]


def test_unreadable_latch_is_unknown_and_does_not_hold(kanban_home, latch, all_assignees_spawnable):
    """Same rule as the overlay: UNKNOWN gate state means no action (never a permanent hold)."""
    latch("garbage")
    spawns: list = []
    with kbc.connect() as conn:
        child = _card_waiting_on_parent(conn, TAG_REASON)
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [child]


def test_unblock_after_tag_clears_the_binding(kanban_home, latch, all_assignees_spawnable):
    """A deliberate unblock (operator or the overlay's release) supersedes the tag, as in the overlay."""
    latch("SWAP_HOT=1")
    spawns: list = []
    with kbc.connect() as conn:
        child = kb.create_task(conn, title="parked", assignee="alice")
        assert kb.claim_task(conn, child) is not None
        assert kb.schedule_task(conn, child, reason=TAG_REASON)
        assert kb.unblock_task(conn, child)
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns))
    assert spawns == [child]


def test_payload_gate_spawn_marker_binds(kanban_home, latch, all_assignees_spawnable):
    """The overlay also accepts payload {"gate": "spawn"}; the guard must agree with it."""
    latch("SWAP_HOT=1")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "dependency_wait", {"reason": "parent", "gate": "spawn"})
        assert kbd.check_respawn_guard(conn, tid) == "spawn_gate_closed"
        # Review lane is never held by this guard.
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None
