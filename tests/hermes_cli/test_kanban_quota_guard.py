"""Quota-pressure-aware kanban dispatch guard (t_58da250a).

Static concurrency caps (``kanban.max_in_progress`` & friends) can't see
provider rate-limit walls: when workers start dying with quota/429 exits
(classified as ``rate_limited`` run outcomes by the reap classifier), the
dispatcher used to keep spawning replacement workers for OTHER tasks on the
same starved provider — burning the same quota the respawn guard was busy
protecting on the individual task.

The quota guard mirrors the memory-pressure guard architecturally:

* ``_quota_pressure_snapshot`` classifies recent ``rate_limited`` run
  outcomes within a sliding window: ok / elevated / critical / unknown.
* critical  -> spawn nothing this tick (deferred, not dropped).
* elevated  -> suppress spawns for the AFFECTED assignee(s) and shrink the
  shared spawn budget by one.
* unknown   -> no restriction (fail-open, same as the memory guard).
* The guard only ever REDUCES below the configured caps, never raises them.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def calm_memory(monkeypatch):
    """Pin the memory guard to 'unknown' (no restriction) so quota-guard
    assertions can't be perturbed by the host's real memory state."""
    monkeypatch.setattr(kb, "_system_memory_sample", lambda: {})


def _guard_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "window_seconds": kb.DEFAULT_QUOTA_GUARD_WINDOW_SECONDS,
        "elevated_threshold": kb.DEFAULT_QUOTA_GUARD_ELEVATED_THRESHOLD,
        "critical_threshold": kb.DEFAULT_QUOTA_GUARD_CRITICAL_THRESHOLD,
        "critical_tick_horizon": kb.DEFAULT_QUOTA_GUARD_CRITICAL_TICK_HORIZON,
    }
    cfg.update(overrides)
    return cfg


def _seed_rate_limited_run(conn, task_id: str, *, ago_seconds: int = 60) -> None:
    """Insert a finished ``rate_limited`` run row, mirroring what
    ``detect_crashed_workers`` persists for an EX_TEMPFAIL worker death."""
    now = int(time.time())
    task = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    profile = task["assignee"] if task and task["assignee"] else "whoever"
    conn.execute(
        "INSERT INTO task_runs "
        "(task_id, profile, status, started_at, ended_at, outcome, error) "
        "VALUES (?, ?, 'rate_limited', ?, ?, 'rate_limited', 'quota wall')",
        (
            task_id,
            profile,
            now - ago_seconds - 30,
            now - ago_seconds,
        ),
    )
    conn.commit()


def _seed_sustained_rate_limits(conn, task_id: str) -> None:
    """Seed one hit in each default quota tick bucket."""
    for ago_seconds in (1, 301, 601):
        _seed_rate_limited_run(conn, task_id, ago_seconds=ago_seconds)


# ---------------------------------------------------------------------------
# _quota_guard_config
# ---------------------------------------------------------------------------


def test_quota_guard_config_defaults(kanban_home):
    cfg = kb._quota_guard_config()
    assert cfg["enabled"] is True
    assert cfg["window_seconds"] == kb.DEFAULT_QUOTA_GUARD_WINDOW_SECONDS
    assert (
        cfg["elevated_threshold"]
        == kb.DEFAULT_QUOTA_GUARD_ELEVATED_THRESHOLD
    )
    assert (
        cfg["critical_threshold"]
        == kb.DEFAULT_QUOTA_GUARD_CRITICAL_THRESHOLD
    )
    assert (
        cfg["critical_tick_horizon"]
        == kb.DEFAULT_QUOTA_GUARD_CRITICAL_TICK_HORIZON
    )
    # Sanity on the shipped defaults themselves.
    assert cfg["elevated_threshold"] < cfg["critical_threshold"]
    assert cfg["window_seconds"] > 0


def test_quota_guard_config_reads_kanban_quota_guard_keys(kanban_home, monkeypatch):
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: {
            "kanban": {
                "quota_guard": {
                    "enabled": False,
                    "window_seconds": 120,
                    "elevated_threshold": 2,
                    "critical_threshold": 5,
                    "critical_tick_horizon": 4,
                }
            }
        },
    )
    cfg = kb._quota_guard_config()
    assert cfg == {
        "enabled": False,
        "window_seconds": 120,
        "elevated_threshold": 2,
        "critical_threshold": 5,
        "critical_tick_horizon": 4,
    }


def test_quota_guard_config_ignores_invalid_values(kanban_home, monkeypatch):
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: {
            "kanban": {
                "quota_guard": {
                    "window_seconds": "soon",
                    "elevated_threshold": -3,
                    "critical_threshold": 0,
                }
            }
        },
    )
    cfg = kb._quota_guard_config()
    assert cfg["window_seconds"] == kb.DEFAULT_QUOTA_GUARD_WINDOW_SECONDS
    assert (
        cfg["elevated_threshold"]
        == kb.DEFAULT_QUOTA_GUARD_ELEVATED_THRESHOLD
    )
    assert (
        cfg["critical_threshold"]
        == kb.DEFAULT_QUOTA_GUARD_CRITICAL_THRESHOLD
    )


# ---------------------------------------------------------------------------
# _quota_pressure_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_ok_with_no_recent_rate_limits(kanban_home):
    with kb.connect() as conn:
        level, hits = kb._quota_pressure_snapshot(conn)
    assert level == "ok"
    assert hits == {}


def test_snapshot_elevated_on_single_recent_hit(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="starved", assignee="alice")
        _seed_rate_limited_run(conn, tid, ago_seconds=60)
        level, hits = kb._quota_pressure_snapshot(conn)
    assert level == "elevated"
    assert hits == {"alice": 1}


def test_snapshot_critical_on_sustained_hits(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="starved", assignee="alice")
        _seed_sustained_rate_limits(conn, tid)
        level, hits = kb._quota_pressure_snapshot(conn)
    assert level == "critical"
    assert hits["alice"] == kb.DEFAULT_QUOTA_GUARD_CRITICAL_THRESHOLD


def test_snapshot_critical_requires_sustained_tick_horizon(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="burst", assignee="alice")
        for _ in range(kb.DEFAULT_QUOTA_GUARD_CRITICAL_THRESHOLD):
            _seed_rate_limited_run(conn, tid, ago_seconds=60)
        level, hits = kb._quota_pressure_snapshot(conn)
    assert level == "elevated"
    assert hits == {"alice": kb.DEFAULT_QUOTA_GUARD_CRITICAL_THRESHOLD}


def test_snapshot_does_not_combine_distinct_profiles_into_critical(kanban_home):
    with kb.connect() as conn:
        for assignee in ("alice", "bob", "carol"):
            tid = kb.create_task(conn, title=assignee, assignee=assignee)
            _seed_rate_limited_run(conn, tid, ago_seconds=60)
        level, hits = kb._quota_pressure_snapshot(conn)
    assert level == "elevated"
    assert hits == {"alice": 1, "bob": 1, "carol": 1}


def test_snapshot_recovers_when_hits_age_out_of_window(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="starved", assignee="alice")
        _seed_rate_limited_run(
            conn, tid,
            ago_seconds=kb.DEFAULT_QUOTA_GUARD_WINDOW_SECONDS + 60,
        )
        level, hits = kb._quota_pressure_snapshot(conn)
    assert level == "ok"
    assert hits == {}


def test_snapshot_unknown_on_query_error(kanban_home):
    class BrokenConn:
        def execute(self, *a, **k):
            raise RuntimeError("db exploded")

    level, hits = kb._quota_pressure_snapshot(BrokenConn())
    assert level == "unknown"
    assert hits == {}


def test_snapshot_disabled_reports_ok(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kb, "_quota_guard_config", lambda: _guard_cfg(enabled=False)
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="starved", assignee="alice")
        for _ in range(10):
            _seed_rate_limited_run(conn, tid, ago_seconds=60)
        level, hits = kb._quota_pressure_snapshot(conn)
    assert level == "ok"
    assert hits == {}


# ---------------------------------------------------------------------------
# dispatch_once under quota pressure
# ---------------------------------------------------------------------------


def test_dispatch_critical_suppresses_affected_and_continues(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_sustained_rate_limits(conn, starved)
        fresh = kb.create_task(conn, title="fresh", assignee="bob")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        row = kb.get_task(conn, fresh)

    assert spawns == [fresh]
    assert res.quota_pressure == "critical"
    # Critical pressure is profile-scoped: alice is deferred while bob's
    # unaffected work continues. Both cases are defer-not-drop.
    assert row is not None and row.status == "running"


def test_dispatch_elevated_suppresses_affected_profile_only(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.assignee)
        return 42

    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_rate_limited_run(conn, starved, ago_seconds=60)
        # A DIFFERENT ready task for the starved profile: the per-task
        # respawn guard can't see it, only budget-level suppression can.
        alice_next = kb.create_task(conn, title="alice-next", assignee="alice")
        kb.create_task(conn, title="bob-work", assignee="bob")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        row = kb.get_task(conn, alice_next)

    assert res.quota_pressure == "elevated"
    assert "bob" in spawns
    assert "alice" not in spawns
    assert (alice_next, "alice") in res.skipped_quota_suppressed
    # Suppressed, not blocked: alice's task stays ready for a later tick.
    assert row is not None and row.status == "ready"


def test_dispatch_elevated_shrinks_spawn_budget_by_one(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_rate_limited_run(conn, starved, ago_seconds=60)
        # Unaffected profiles queue up 3 tasks; cap would allow 2 —
        # elevated pressure shrinks the shared budget to 1.
        kb.create_task(conn, title="b", assignee="bob")
        kb.create_task(conn, title="c", assignee="carol")
        kb.create_task(conn, title="d", assignee="dave")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=2)

    assert res.quota_pressure == "elevated"
    assert len(spawns) == 1


def test_dispatch_elevated_never_widens_an_exhausted_budget(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    """The guard only reduces: a cap already at 0 remaining stays 0."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_rate_limited_run(conn, starved, ago_seconds=60)
        running = kb.create_task(conn, title="running", assignee="bob")
        kb.claim_task(conn, running)
        kb.create_task(conn, title="ready", assignee="carol")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=1)

    assert not spawns
    assert not res.spawned


def test_dispatch_unknown_pressure_imposes_no_restriction(
    kanban_home, all_assignees_spawnable, calm_memory, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_quota_pressure_snapshot", lambda conn: ("unknown", {})
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 3
    assert res.quota_pressure is None


def test_dispatch_disabled_guard_imposes_no_restriction(
    kanban_home, all_assignees_spawnable, calm_memory, monkeypatch,
):
    monkeypatch.setattr(
        kb, "_quota_guard_config", lambda: _guard_cfg(enabled=False)
    )
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        for _ in range(10):
            _seed_rate_limited_run(conn, starved, ago_seconds=60)
        kb.create_task(conn, title="fresh", assignee="bob")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert res.quota_pressure is None
    assert any(True for _ in spawns)


def test_dispatch_recovers_after_window_expiry(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    """Scale-up path: once the window is clean, the configured caps apply
    unchanged — no sticky throttle state survives the window."""
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.assignee)
        return 42

    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_rate_limited_run(
            conn, starved,
            ago_seconds=kb.DEFAULT_QUOTA_GUARD_WINDOW_SECONDS + 60,
        )
        kb.create_task(conn, title="alice-next", assignee="alice")
        kb.create_task(conn, title="bob-work", assignee="bob")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert res.quota_pressure is None
    assert not res.skipped_quota_suppressed
    # The previously-starved profile spawns again once the window clears.
    assert "alice" in spawns and "bob" in spawns


def test_dispatch_critical_profile_does_not_freeze_unaffected_profile(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.assignee)
        return 42

    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_sustained_rate_limits(conn, starved)
        alice_next = kb.create_task(conn, title="alice-next", assignee="alice")
        bob_work = kb.create_task(conn, title="bob-work", assignee="bob")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)
        alice_row = kb.get_task(conn, alice_next)
        bob_row = kb.get_task(conn, bob_work)

    assert res.quota_pressure == "critical"
    assert "alice" not in spawns
    assert "bob" in spawns
    assert alice_row is not None and alice_row.status == "ready"
    assert bob_row is not None and bob_row.status == "running"


def test_dispatch_distinct_profile_hits_do_not_board_freeze(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.assignee)
        return 42

    with kb.connect() as conn:
        for assignee in ("alice", "bob", "carol"):
            hit = kb.create_task(conn, title=f"hit-{assignee}", assignee=assignee)
            _seed_rate_limited_run(conn, hit, ago_seconds=60)
        unaffected = kb.create_task(conn, title="unaffected", assignee="dave")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=1)
        row = kb.get_task(conn, unaffected)

    assert res.quota_pressure == "elevated"
    assert spawns == ["dave"]
    assert row is not None and row.status == "running"


def test_dispatch_critical_still_runs_reclaim_bookkeeping(
    kanban_home, all_assignees_spawnable, calm_memory,
):
    """Critical quota pressure must only stop NEW spawns — promotion
    bookkeeping still runs (same contract as the memory guard)."""
    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_sustained_rate_limits(conn, starved)
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(
            conn, title="child", assignee="alice", parents=[parent],
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 42)
        row = kb.get_task(conn, child)

    assert res.quota_pressure == "critical"
    assert row is not None
    assert row.status == "ready"


def test_memory_guard_takes_precedence_over_quota_guard(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Critical memory pressure returns before the quota guard runs; the
    telemetry names the memory guard as the restricting party."""
    GIB = 1024 * 1024
    monkeypatch.setattr(
        kb, "_system_memory_sample",
        lambda: {"mem_available_kib": 32 * 1024, "mem_total_kib": 1 * GIB},
    )
    with kb.connect() as conn:
        starved = kb.create_task(conn, title="starved", assignee="alice")
        _seed_rate_limited_run(conn, starved, ago_seconds=60)
        kb.create_task(conn, title="fresh", assignee="bob")
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 42)

    assert not res.spawned
    assert res.memory_pressure == "critical"
