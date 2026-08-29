"""CPU-load-aware kanban dispatch backoff (t_886aca25).

The memory-pressure guard can't see CPU starvation: spark-4be3 runs
oversubscribed (load 40-47 on 20 cores, up to 96-115 after reboot) while
memory still looks healthy, cron subprocesses get claimed but stall
pre-spawn under starvation, and the liveness monitor fires recovery waves.
This guard defers NEW worker spawns when the 1-minute load average is high
relative to the logical core count:

- load > 1.5 x nproc  -> "elevated"  -> at most 1 new worker this tick
- load > 2.5 x nproc  -> "critical"  -> spawn nothing this tick
- unknown (unreadable loadavg / unknown core count) -> no restriction

Covers the acceptance criteria: deferred-not-dropped, strict threshold
boundaries, config override (``kanban.max_cpu_load_factor``), and
fail-open behaviour. Mirrors the memory-guard test file.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# _cpu_load_level — threshold boundaries
# ---------------------------------------------------------------------------


def test_cpu_level_ok_when_load_below_elevated():
    assert kb._cpu_load_level(load=0.0, nproc=4) == "ok"
    # ratio 1.475 < 1.5 elevated threshold.
    assert kb._cpu_load_level(load=5.9, nproc=4) == "ok"


def test_cpu_level_strict_at_elevated_boundary():
    # ratio exactly 1.5 (6.0/4): strictly-greater semantics -> still ok,
    # matching the "load > 1.5 x nproc" wording (boundary fails open).
    assert kb._cpu_load_level(load=6.0, nproc=4) == "ok"


def test_cpu_level_elevated_above_1_5x():
    assert kb._cpu_load_level(load=6.1, nproc=4) == "elevated"
    assert kb._cpu_load_level(load=9.9, nproc=4) == "elevated"


def test_cpu_level_strict_at_critical_boundary():
    # ratio exactly 2.5 (10.0/4): above elevated but not above critical.
    assert kb._cpu_load_level(load=10.0, nproc=4) == "elevated"


def test_cpu_level_critical_above_2_5x():
    assert kb._cpu_load_level(load=10.1, nproc=4) == "critical"
    assert kb._cpu_load_level(load=40.0, nproc=4) == "critical"


def test_cpu_level_single_core_box():
    # 1 core: load 2.0 -> ratio 2.0 -> elevated; load 3.0 -> critical.
    assert kb._cpu_load_level(load=1.0, nproc=1) == "ok"
    assert kb._cpu_load_level(load=2.0, nproc=1) == "elevated"
    assert kb._cpu_load_level(load=3.0, nproc=1) == "critical"


# ---------------------------------------------------------------------------
# _cpu_load_level — fail-open (unknown imposes no restriction)
# ---------------------------------------------------------------------------


@pytest.mark.real_cpu_guard
def test_cpu_level_unknown_on_unreadable_loadavg(monkeypatch):
    monkeypatch.setattr(kb.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError))
    assert kb._cpu_load_level() == "unknown"


def test_cpu_level_unknown_when_core_count_unknown(monkeypatch):
    monkeypatch.setattr(kb.os, "cpu_count", lambda: None)
    assert kb._cpu_load_level(load=40.0) == "unknown"


def test_cpu_level_unknown_on_zero_core_count():
    assert kb._cpu_load_level(load=10.0, nproc=0) == "unknown"


def test_cpu_level_unknown_on_non_numeric_input():
    assert kb._cpu_load_level(load="abc", nproc=4) == "unknown"  # type: ignore[arg-type]
    assert kb._cpu_load_level(load=10.0, nproc="four") == "unknown"  # type: ignore[arg-type]


def test_cpu_level_unknown_on_negative_or_infinite_ratio():
    assert kb._cpu_load_level(load=-1.0, nproc=4) == "unknown"
    assert kb._cpu_load_level(load=float("inf"), nproc=4) == "unknown"


# ---------------------------------------------------------------------------
# configured_cpu_load_factor — config override (kanban.max_cpu_load_factor)
# ---------------------------------------------------------------------------


def test_cpu_load_factor_default_when_unset(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"kanban": {}}
    )
    assert kb.configured_cpu_load_factor() == kb.CPU_LOAD_ELEVATED_FACTOR


def test_cpu_load_factor_accepts_valid_override(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"max_cpu_load_factor": 2.0}},
    )
    assert kb.configured_cpu_load_factor() == 2.0


def test_cpu_load_factor_rejects_invalid_values(monkeypatch):
    for bad in (0.5, 0, -3, "abc", None, float("nan"), float("inf")):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"kanban": {"max_cpu_load_factor": bad}},
        )
        assert kb.configured_cpu_load_factor() == kb.CPU_LOAD_ELEVATED_FACTOR


def test_cpu_load_factor_falls_back_on_config_read_error(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: (_ for _ in ()).throw(RuntimeError)
    )
    assert kb.configured_cpu_load_factor() == kb.CPU_LOAD_ELEVATED_FACTOR


def test_cpu_level_respects_configured_factor(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"kanban": {"max_cpu_load_factor": 2.0}},
    )
    # ratio 1.9 -> under the configured 2.0 elevated threshold.
    assert kb._cpu_load_level(load=7.6, nproc=4) == "ok"
    # ratio 2.1 -> elevated.
    assert kb._cpu_load_level(load=8.4, nproc=4) == "elevated"
    # ratio 3.1 -> critical (elevated 2.0 + critical delta 1.0).
    assert kb._cpu_load_level(load=12.4, nproc=4) == "critical"


# ---------------------------------------------------------------------------
# dispatch_once under CPU load
# ---------------------------------------------------------------------------


def _cpu_level_stub(level):
    """Patch the guard classifier so dispatch tests control it directly."""
    return lambda: level


def test_dispatch_spawns_nothing_under_critical_cpu(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(kb, "_cpu_load_level", _cpu_level_stub("critical"))
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert not spawns
    assert not res.spawned
    assert res.cpu_load == "critical"


def test_dispatch_critical_cpu_defers_not_drops(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """Tasks skipped under CPU load stay 'ready' and spawn once it clears."""
    level = {"value": "critical"}
    monkeypatch.setattr(kb, "_cpu_load_level", lambda: level["value"])
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        task = kb.create_task(conn, title="a", assignee="alice")
        kb.dispatch_once(conn, spawn_fn=fake_spawn)
        assert not spawns
        row = kb.get_task(conn, task)
        assert row is not None and row.status == "ready"

        level["value"] = "ok"
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert spawns == [task]
    assert res.cpu_load is None


def test_dispatch_elevated_cpu_spawns_at_most_one(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(kb, "_cpu_load_level", _cpu_level_stub("elevated"))
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 1
    assert res.cpu_load == "elevated"


def test_dispatch_elevated_cpu_does_not_widen_tighter_budget(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """A caller cap already at 0 remaining must not be widened to 1."""
    monkeypatch.setattr(kb, "_cpu_load_level", _cpu_level_stub("elevated"))
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, running)
        kb.create_task(conn, title="ready", assignee="bob")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=1)

    assert not spawns
    assert not res.spawned


def test_dispatch_unknown_cpu_imposes_no_restriction(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    monkeypatch.setattr(kb, "_cpu_load_level", _cpu_level_stub("unknown"))
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 3
    assert res.cpu_load is None


@pytest.mark.real_cpu_guard
def test_dispatch_fails_open_when_loadavg_unreadable(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """End-to-end fail-open: a real getloadavg failure -> no restriction."""
    monkeypatch.setattr(kb.os, "getloadavg", lambda: (_ for _ in ()).throw(OSError))
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kb.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kb.dispatch_once(conn, spawn_fn=fake_spawn)

    assert len(spawns) == 3
    assert res.cpu_load is None


def test_dispatch_critical_cpu_still_runs_reclaim_bookkeeping(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The guard must only stop NEW spawns — reclaim/promotion still run."""
    monkeypatch.setattr(kb, "_cpu_load_level", _cpu_level_stub("critical"))
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        child = kb.create_task(
            conn, title="child", assignee="alice", parents=[parent],
        )
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        res = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: 42)
        row = kb.get_task(conn, child)

    assert res.cpu_load == "critical"
    assert row is not None
    assert row.status == "ready"
