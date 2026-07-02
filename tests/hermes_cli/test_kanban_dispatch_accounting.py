"""Dispatcher accounting invariants for ready/blocked dispatch buckets.

A dispatch tick must make every ready row visible as either spawned or in
exactly one skip/defer bucket. Non-ready dependency/time-gated rows are reported
separately so dry-run/live summaries explain why the queue is not spawnable
without mutating those rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(board="default")
    return home


@pytest.fixture
def conn(kanban_home: Path):
    with kb.connect(board="default") as c:
        yield c


def _fake_spawn(task, workspace_path, board=None):
    return 12345


def _set_ready_failure(conn, task_id: str, error: str) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready', last_failure_error = ? WHERE id = ?",
            (error, task_id),
        )


def _assert_ready_accounted_once(conn, result) -> None:
    ready_ids = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM tasks WHERE status = 'ready' AND claim_lock IS NULL"
        )
    }
    buckets: dict[str, list[str]] = {
        "spawned": [tid for tid, _assignee, _ws in result.spawned],
        "skipped_unassigned": list(result.skipped_unassigned),
        "skipped_nonspawnable": list(result.skipped_nonspawnable),
        "skipped_per_profile_capped": [tid for tid, _assignee, _current in result.skipped_per_profile_capped],
        "respawn_guarded": [tid for tid, _reason in result.respawn_guarded],
        "deferred_global_capped": [tid for tid, _assignee, _reason in result.deferred_global_capped],
        "skipped_claim_race": list(result.skipped_claim_race),
    }
    seen: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}
    for bucket, ids in buckets.items():
        for task_id in ids:
            if task_id in seen:
                duplicates.setdefault(task_id, [seen[task_id]]).append(bucket)
            seen[task_id] = bucket
    assert duplicates == {}
    assert ready_ids <= set(seen), f"missing accounting for ready ids: {ready_ids - set(seen)}"


def test_provider_capacity_respawn_guard_is_visible_and_exclusive(conn, all_assignees_spawnable):
    guarded = kb.create_task(conn, title="provider quota", assignee="worker")
    normal = kb.create_task(conn, title="normal", assignee="worker")
    _set_ready_failure(conn, guarded, "OpenAI 429 rate limit: provider capacity exhausted")

    result = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert (guarded, "blocker_auth") in result.respawn_guarded
    assert [tid for tid, _assignee, _ws in result.spawned] == [normal]
    _assert_ready_accounted_once(conn, result)


def test_nonspawnable_terminal_lane_is_visible_and_exclusive(conn, monkeypatch):
    from hermes_cli import profiles

    lane = kb.create_task(conn, title="terminal lane", assignee="orion-cc")
    normal = kb.create_task(conn, title="normal", assignee="worker")
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "worker")

    result = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert result.skipped_nonspawnable == [lane]
    assert [tid for tid, _assignee, _ws in result.spawned] == [normal]
    _assert_ready_accounted_once(conn, result)


def test_per_profile_cap_bucket_is_visible_and_exclusive(conn, all_assignees_spawnable):
    running = kb.create_task(conn, title="running alpha", assignee="alpha")
    claimed_running = kb.claim_task(conn, running)
    assert claimed_running is not None
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (__import__("os").getpid(), running),
        )
    capped = kb.create_task(conn, title="alpha capped", assignee="alpha")
    spawnable = kb.create_task(conn, title="beta spawnable", assignee="beta")

    result = kb.dispatch_once(
        conn,
        spawn_fn=_fake_spawn,
        dry_run=True,
        max_in_progress_per_profile=1,
    )

    assert result.skipped_per_profile_capped == [(capped, "alpha", 1)]
    assert result.spawned == [(spawnable, "beta", "")]
    assert result.deferred_global_capped == []
    _assert_ready_accounted_once(conn, result)


def test_global_cap_bucket_is_visible_and_exclusive(conn, all_assignees_spawnable):
    spawnable = kb.create_task(conn, title="beta spawnable", assignee="beta")
    global_deferred = kb.create_task(conn, title="gamma global deferred", assignee="gamma")

    result = kb.dispatch_once(
        conn,
        spawn_fn=_fake_spawn,
        dry_run=True,
        max_spawn=1,
    )

    assert result.spawned == [(spawnable, "beta", "")]
    assert result.deferred_global_capped == [(global_deferred, "gamma", "max_spawn")]
    _assert_ready_accounted_once(conn, result)


def test_dependency_and_time_gate_rows_are_reported_without_spawning(conn):
    blocked_parent = kb.create_task(conn, title="parent", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (blocked_parent,))
    child = kb.create_task(conn, title="child", assignee="worker", parents=[blocked_parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
    scheduled = kb.create_task(conn, title="scheduled", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (scheduled,))

    result = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)

    assert result.deferred_dependency == [(child, [blocked_parent])]
    assert result.deferred_scheduled == [scheduled]
    assert result.spawned == []
    assert result.skipped_unassigned == []
    assert result.skipped_nonspawnable == []


def test_empty_and_healthy_queues_have_no_hidden_buckets(conn, all_assignees_spawnable):
    empty = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert empty.spawned == []
    assert empty.skipped_unassigned == []
    assert empty.skipped_nonspawnable == []
    assert empty.skipped_per_profile_capped == []
    assert empty.respawn_guarded == []
    assert empty.deferred_global_capped == []
    assert empty.deferred_dependency == []
    assert empty.deferred_scheduled == []

    t1 = kb.create_task(conn, title="one", assignee="worker")
    t2 = kb.create_task(conn, title="two", assignee="worker")
    healthy = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True)
    assert healthy.spawned == [(t1, "worker", ""), (t2, "worker", "")]
    assert healthy.deferred_global_capped == []
    _assert_ready_accounted_once(conn, healthy)
