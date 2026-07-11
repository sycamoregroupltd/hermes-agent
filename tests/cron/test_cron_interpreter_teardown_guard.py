"""Regression tests for t_dfd9b303.

Root cause: a gateway restart/upgrade at 2026-07-10 06:39 flipped CPython's
process-wide interpreter-shutdown flag while 6 cron jobs were in flight. Any
``ThreadPoolExecutor.submit`` (or joblib/loky ProcessPoolExecutor.submit) that
fires in that window raises ``RuntimeError('cannot schedule new futures after
interpreter shutdown')``. Uncaught, it aborted the whole ``tick()`` and
recorded that teardown error into every due job's ``last_error``, so a routine
restart looked like 5-6 independent job failures.

These tests pin the fix:
  * ``_submit_to_pool`` swallows the interpreter-shutdown RuntimeError and
    returns ``None`` (job skipped; will re-fire on next tick) instead of
    crashing the tick.
  * ``_register_shutdown_sentinel`` flips the module-level
    ``_interpreter_shutting_down`` flag via ``atexit`` so the guard can
    short-circuit even before the first submit is attempted.
"""

from __future__ import annotations

import concurrent.futures

import pytest


def _load():
    import cron.scheduler as sched

    return sched


def test_submit_to_pool_returns_future_on_normal_submit():
    sched = _load()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = sched._submit_to_pool(pool, lambda: 42)
        assert fut is not None
        assert fut.result() == 42
    finally:
        pool.shutdown(wait=True)


def test_submit_to_pool_swallows_interpreter_shutdown(monkeypatch):
    sched = _load()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        # Simulate the exact CPython teardown RuntimeError.
        def _boom(*a, **k):
            raise RuntimeError("cannot schedule new futures after interpreter shutdown")

        monkeypatch.setattr(pool, "submit", _boom)
        assert sched._submit_to_pool(pool, lambda: 1) is None
    finally:
        pool.shutdown(wait=True)


def test_submit_to_pool_reraises_other_runtime_error(monkeypatch):
    sched = _load()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:

        def _boom(*a, **k):
            raise RuntimeError("something else broke")

        monkeypatch.setattr(pool, "submit", _boom)
        with pytest.raises(RuntimeError):
            sched._submit_to_pool(pool, lambda: 1)
    finally:
        pool.shutdown(wait=True)


def test_shutdown_sentinel_registered_as_atexit():
    sched = _load()
    # The sentinel is a module-level bool and an atexit hook is registered.
    assert isinstance(sched._interpreter_shutting_down, bool)
    # When atexit callbacks run (process exit), the flag flips True. We cannot
    # easily invoke atexit here without exiting, so assert the helper exists
    # and flips the flag when called directly.
    sched._interpreter_shutting_down = False
    sched._register_shutdown_sentinel()
    assert sched._interpreter_shutting_down is True
    # restore for any later use in the process
    sched._interpreter_shutting_down = False


def test_submit_to_pool_short_circuits_when_shutting_down(monkeypatch):
    sched = _load()
    sched._interpreter_shutting_down = True
    try:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            # submit must never be reached; if it is, this would actually queue.
            called = {"hit": False}

            def _should_not_be_called(*a, **k):
                called["hit"] = True
                raise AssertionError("submit reached during shutdown")

            monkeypatch.setattr(pool, "submit", _should_not_be_called)
            assert sched._submit_to_pool(pool, lambda: 1) is None
            assert called["hit"] is False
        finally:
            pool.shutdown(wait=True)
    finally:
        sched._interpreter_shutting_down = False


def test_teardown_guard_alerts_on_interpreter_shutdown(monkeypatch):
    """Site A: a teardown RuntimeError MUST now emit a #critical-alerts blast.

    Regression for the silent-logger.warning gap. The reused helper
    ``_alert_critical_alerts`` is monkeypatched so we can assert the blast fired.
    """
    sched = _load()
    sched._teardown_alert_emitted = False
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        captured = {"message": None}

        def _boom(*a, **k):
            raise RuntimeError(
                "cannot schedule new futures after interpreter shutdown"
            )

        monkeypatch.setattr(pool, "submit", _boom)
        monkeypatch.setattr(
            sched, "_alert_critical_alerts",
            lambda msg: captured.__setitem__("message", msg),
        )
        assert sched._submit_to_pool(pool, lambda: 1) is None
        assert captured["message"] is not None
        assert "interpreter teardown" in captured["message"]
        assert "t_dfd9b303" in captured["message"]
    finally:
        pool.shutdown(wait=True)
        sched._teardown_alert_emitted = False


def test_teardown_guard_no_alert_on_normal_submit(monkeypatch):
    """Site A: a normal successful submit MUST NOT emit any #critical-alerts blast."""
    sched = _load()
    sched._teardown_alert_emitted = False
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        called = {"hit": False}

        def _spy(msg):
            called["hit"] = True

        monkeypatch.setattr(sched, "_alert_critical_alerts", _spy)
        fut = sched._submit_to_pool(pool, lambda: 7)
        assert fut is not None
        assert fut.result() == 7
        assert called["hit"] is False
    finally:
        pool.shutdown(wait=True)
        sched._teardown_alert_emitted = False


def test_emit_teardown_alert_is_debounced(monkeypatch):
    """The blast fires at most ONCE per teardown window (debounce flag)."""
    sched = _load()
    sched._teardown_alert_emitted = False
    calls = []

    def _spy(msg):
        calls.append(msg)

    monkeypatch.setattr(sched, "_alert_critical_alerts", _spy)
    sched._emit_teardown_alert("t_dfd9b303")
    sched._emit_teardown_alert("t_dfd9b303")
    sched._emit_teardown_alert("t_dfd9b303")
    assert len(calls) == 1
    sched._teardown_alert_emitted = False
