"""Focused tests for the gateway dispatcher watcher's distinct
missing-exit-signal telemetry.

These cover the two behaviors the dispatcher review round requires:

* A tick that ONLY reconciles ``missing_exit_signal`` tasks (``spawned`` is
  empty) MUST emit a distinct, searchable ``missing_exit_signal=N`` log line —
  not stay silent and not be counted as a generic crash.
* A tick with nothing to reconcile AND nothing spawned MUST stay silent — a
  detector that always fires is as useless as one that never does.

The decision is extracted into the pure helper ``_dispatcher_tick_log`` so it
is testable without driving the live async ``_kanban_dispatcher_watcher`` loop
(which would require a full gateway bootstrap). ``_tick_log_records`` mirrors
the watcher's per-board loop so the empty-spawn and idle-tick behaviours are
proved at the same shape the production loop uses.
"""

from __future__ import annotations

import logging
from typing import Any

from gateway import kanban_watchers
from gateway.kanban_watchers import _dispatcher_tick_considered, _dispatcher_tick_log
from hermes_cli.kanban_db import DispatchResult


def _result(**overrides: Any) -> DispatchResult:
    defaults: dict[str, Any] = dict(
        reclaimed=0,
        promoted=0,
        spawned=[],
        crashed=[],
        auto_blocked=[],
        missing_exit_signal=[],
        timed_out=[],
        stale=[],
    )
    defaults.update(overrides)
    return DispatchResult(**defaults)


def _tick_log_records(results):
    """Mirror the dispatcher watcher's per-board telemetry loop.

    Returns ``(any_spawned, records)`` where ``records`` are the rendered
    ``(slug, line)`` pairs the loop would log. This is the same shape as the
    loop in ``gateway/kanban_watchers.py`` (``_kanban_dispatcher_watcher``),
    minus the async/to_thread plumbing.
    """
    any_spawned = False
    records = []
    for slug, res in results or []:
        should_log, message, args = _dispatcher_tick_log(slug, res)
        if not should_log:
            continue
        any_spawned = True
        records.append((slug, message % args))
    return any_spawned, records


def test_empty_spawn_missing_exit_signal_tick_fires_distinct_line():
    """A tick with spawned=[] but missing_exit_signal=[...] must log a
    distinct line — NOT stay silent and NOT masquerade as a generic crash."""
    should_log, message, args = _dispatcher_tick_log(
        "jarvis-os", _result(missing_exit_signal=["t_missing"])
    )
    assert should_log is True
    assert "missing_exit_signal=%d" in message
    rendered = message % args
    assert "spawned=0" in rendered
    assert "missing_exit_signal=1" in rendered
    # The distinct bucket is what makes the diagnostic searchable and keeps
    # it from being misread as a crash-family count.
    assert "crashed=0" in rendered


def test_empty_spawn_tick_emits_searchable_line_via_real_logger(caplog):
    """Drive the actual ``gateway.run`` logger so the rendered record proves
    the production log path carries the distinct bucket."""
    should_log, message, args = _dispatcher_tick_log(
        "jarvis-os", _result(missing_exit_signal=["t_missing", "t_missing2"])
    )
    assert should_log is True
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        kanban_watchers.logger.info(message, *args)
    lines = [r.getMessage() for r in caplog.records]
    assert any("missing_exit_signal=2" in line and "spawned=0" in line for line in lines)
    assert any("kanban dispatcher [jarvis-os]" in line for line in lines)


def test_idle_tick_stays_quiet():
    """A genuinely idle tick — nothing spawned, nothing reconciled — must not
    emit spurious telemetry."""
    should_log, _, _ = _dispatcher_tick_log("jarvis-os", _result())
    assert should_log is False


def test_none_result_stays_quiet():
    should_log, _, _ = _dispatcher_tick_log("jarvis-os", None)
    assert should_log is False


def test_spawned_only_tick_keeps_legacy_line():
    """Backward compatibility: a plain spawn tick still logs the legacy line
    WITHOUT the missing_exit_signal bucket, so existing consumers keying on
    the old shape do not regress."""
    should_log, message, args = _dispatcher_tick_log(
        "jarvis-os", _result(spawned=[("t1", "worker", "/tmp/ws")])
    )
    assert should_log is True
    assert "missing_exit_signal" not in message
    rendered = message % args
    assert "spawned=1" in rendered


def test_mixed_tick_reports_both_buckets():
    """A tick that both spawns and reconciles missing-exit-signal tasks logs
    the distinct line with both counts present."""
    should_log, message, args = _dispatcher_tick_log(
        "jarvis-os",
        _result(
            spawned=[("t1", "worker", "/tmp/ws")],
            missing_exit_signal=["t_missing"],
        ),
    )
    assert should_log is True
    rendered = message % args
    assert "spawned=1" in rendered
    assert "missing_exit_signal=1" in rendered


def test_loop_only_logs_busy_board_among_idle_boards():
    """Per-board decision: an idle sibling board must not log a spurious zero
    line just because another board in the same tick spawned."""
    any_spawned, records = _tick_log_records(
        [
            ("busy", _result(spawned=[("t1", "worker", "/tmp/ws")])),
            ("idle", _result()),
            ("idle2", None),
        ]
    )
    assert any_spawned is True
    assert [slug for slug, _ in records] == ["busy"]


def test_loop_empty_spawn_missing_tick_fires_and_counts_as_activity():
    """The empty-spawn case at loop level: only-missing-exit-signal boards log
    the distinct line and count as dispatcher activity (reset the stuck
    detector), proving a missing-exit reconciliation tick is not silent."""
    any_spawned, records = _tick_log_records(
        [
            ("jarvis-os", _result(missing_exit_signal=["t_missing"])),
            ("sycode", _result()),
        ]
    )
    assert any_spawned is True
    assert len(records) == 1
    slug, line = records[0]
    assert slug == "jarvis-os"
    assert "missing_exit_signal=1" in line
    assert "spawned=0" in line


def test_loop_all_idle_tick_stays_quiet():
    any_spawned, records = _tick_log_records(
        [
            ("jarvis-os", _result()),
            ("sycode", _result()),
        ]
    )
    assert any_spawned is False
    assert records == []


def test_loop_respawn_guarded_tick_counts_as_considered():
    """A deliberately guarded ready task is considered work, not a stuck
    dispatcher with zero activity (regression for active_pr guards)."""
    results = [
        ("jarvis-os", _result(respawn_guarded=[("t_pr", "active_pr")])),
    ]
    any_spawned, records = _tick_log_records(results)
    assert any_spawned is False
    assert records == []
    assert _dispatcher_tick_considered(results) is True


def test_loop_mixed_guarded_and_unguarded_ready_stays_unconsidered():
    """A guard on one board must not hide unspawned spawnable work elsewhere."""
    results = [
        ("jarvis-os", _result(respawn_guarded=[("t_pr", "active_pr")])),
        ("sycode", _result()),
    ]
    pending = {
        ("jarvis-os", "t_pr"),
        ("sycode", "t_unspawned"),
    }
    assert _dispatcher_tick_considered(results, pending) is False
    assert _dispatcher_tick_considered(results, {("jarvis-os", "t_pr")}) is True
