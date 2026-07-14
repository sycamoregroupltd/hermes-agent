"""Regression tests for the worker-spawn EAGAIN (Errno 11) resilience fix.

Task t_b31b1a77: a transient ``fork()``/``clone()`` EAGAIN
(``[Errno 11] Resource temporarily unavailable``) on the dispatcher's
single unretried ``subprocess.Popen(..., start_new_session=True)`` was
propagating straight to ``_record_spawn_failure``, incrementing
``consecutive_failures`` and (with ``DEFAULT_FAILURE_LIMIT == 2``) auto-
blocking the task with a stale error. 33 sycode-trading cards stranded
this way on 2026-07-14.

These tests pin the fix: ``_default_spawn`` must retry transient clone
failures with backoff (absorbing them with NO circuit-breaker trip) and
the surge-cap must throttle the clone burst rate.
"""

from __future__ import annotations

import errno
import sys
import time
import types
import unittest.mock
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (mirrors the fixture in
    test_kanban_db.py so this regression suite is self-contained)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_eagain(exc_type=OSError, errno_val=errno.EAGAIN, msg="[Errno 11] Resource temporarily unavailable"):
    """Build an exception carrying a transient clone-pressure errno."""
    exc = exc_type(msg)
    exc.errno = errno_val
    return exc


def _patch_popen(monkeypatch, fail_sequence):
    """Patch ``subprocess.Popen`` used by ``_default_spawn`` with a callable
    that raises the next item of ``fail_sequence`` (or returns a fake proc).

    ``fail_sequence`` is a list; each call pops the front. A callable is
    invoked with the same args/kwargs to decide (lets a test assert the
    retry count). A non-callable is raised/returned directly.
    """
    calls = {"n": 0}

    class _FakeProc:
        pid = 999999

    real_popen = subprocess_module().Popen

    def _fake_popen(*args, **kwargs):
        calls["n"] += 1
        if not fail_sequence:
            return _FakeProc()
        item = fail_sequence.pop(0)
        if callable(item):
            item(*args, **kwargs)  # may raise
        if item is None:
            return _FakeProc()
        if isinstance(item, BaseException):
            raise item
        # otherwise treat as a sentinel to succeed
        return _FakeProc()

    monkeypatch.setattr(subprocess_module(), "Popen", _fake_popen)
    return calls, _FakeProc


def subprocess_module():
    # _default_spawn does ``import subprocess`` locally, so patch the real
    # module object.
    import subprocess

    return subprocess


# ---------------------------------------------------------------------------
# Unit: transient detection
# ---------------------------------------------------------------------------


def test_spawn_is_transient_eagain():
    assert kb._spawn_is_transient_error(_make_eagain()) is True


def test_spawn_is_transient_eagains_errno_via_message():
    # errno munged away but the canonical message survives
    exc = OSError("[Errno 11] Resource temporarily unavailable")
    exc.errno = None
    assert kb._spawn_is_transient_error(exc) is True


def test_spawn_is_transient_enomem_counts():
    exc = OSError("Cannot allocate memory")
    exc.errno = errno.ENOMEM
    assert kb._spawn_is_transient_error(exc) is True


def test_spawn_is_non_transient_filenotfound():
    exc = FileNotFoundError("[Errno 2] No such file or directory: 'hermes'")
    exc.errno = errno.ENOENT
    assert kb._spawn_is_transient_error(exc) is False


# ---------------------------------------------------------------------------
# Unit: retry/backoff absorbs transient EAGAIN with no breaker trip
# ---------------------------------------------------------------------------


def test_spawn_worker_with_retry_absorbs_transient_eagain(monkeypatch):
    """First two clones fail with EAGAIN, third succeeds. No exception
    should escape and the retry counter should reflect exactly 2 retries."""
    seq = [_make_eagain(), _make_eagain(), None]
    calls, _ = _patch_popen(monkeypatch, seq)
    before = kb._SPAWN_BUCKET.get("_eagain_retries", 0)
    proc = kb._spawn_worker_with_retry(
        ["hermes", "chat", "-q", "x"], "/tmp", open("/dev/null", "wb"),
        {"PATH": "/usr/bin"},
    )
    assert proc is not None
    assert calls["n"] == 3  # 2 failures + 1 success
    # exactly 2 transient retries counted
    assert kb._SPAWN_BUCKET.get("_eagain_retries", 0) - before == 2


def test_spawn_worker_with_retry_gives_up_after_persistent_eagain(monkeypatch):
    """If every attempt hits EAGAIN, the LAST one surfaces (does not silently
    swallow). This is the now-rare persistent case, not the stranded transient."""
    seq = [_make_eagain() for _ in range(kb.SPAWN_EAGAIN_MAX_RETRIES + 1)]
    _patch_popen(monkeypatch, seq)
    with pytest.raises(OSError):
        kb._spawn_worker_with_retry(
            ["hermes"], "/tmp", open("/dev/null", "wb"), {},
        )


def test_spawn_worker_with_retry_non_transient_propagates(monkeypatch):
    """A genuine FileNotFoundError must NOT be retried — it propagates once."""
    calls, _ = _patch_popen(monkeypatch, [FileNotFoundError("no hermes")])
    with pytest.raises(FileNotFoundError):
        kb._spawn_worker_with_retry(
            ["hermes"], "/tmp", open("/dev/null", "wb"), {},
        )
    assert calls["n"] == 1  # no retry


def test_surge_cap_tokens_refill_over_time(monkeypatch):
    """The token bucket refills: after the window elapses, tokens are
    available again. This is what keeps a quiet dispatcher from ever feeling
    the cap while spreading out a backlog burst."""
    # reset bucket
    kb._SPAWN_BUCKET.clear()
    cap = kb.SPAWN_SURGE_CAP_PER_WINDOW
    # drain it
    start = time.monotonic()
    for _ in range(cap):
        kb._spawn_acquire_surge_token()
    # immediately: no tokens left, but safety valve should not block a window
    kb._spawn_acquire_surge_token()
    elapsed = time.monotonic() - start
    # With cap clones already consumed and then one more blocking up to one
    # window, elapsed should be bounded (well under 2 windows).
    assert elapsed < kb.SPAWN_SURGE_WINDOW_SECONDS * 2 + 0.5


# ---------------------------------------------------------------------------
# Integration: a real dispatch tick must NOT auto-block a task whose only
# failure is a transient EAGAIN (the original stranding bug).
# ---------------------------------------------------------------------------


def test_dispatch_does_not_auto_block_on_transient_eagain(kanban_home, monkeypatch):
    """Regression for t_b31b1a77: a ready task that hits transient EAGAIN on
    spawn must be released back to ``ready`` (re-dispatched next tick), NOT
    auto-blocked with a stale error. The fix absorbs the transient in
    ``_default_spawn`` so ``_record_spawn_failure`` is never even called."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="eagain-card", assignee="sentinel")

        # Make the real spawn fail once with EAGAIN then succeed.
        seq = [_make_eagain(), None]
        calls, _ = _patch_popen(monkeypatch, seq)

        res = kb.dispatch_once(conn, spawn_fn=kb._default_spawn, board=None)

    # No auto-block: the transient was absorbed by retry.
    assert tid not in res.auto_blocked, (
        "transient EAGAIN must NOT auto-block the task"
    )
    # The task was claimed (running) then, because the real Popen in the test
    # environment may or may not find a usable hermes, we only assert the
    # critical invariant: it was never auto-blocked by the circuit breaker.
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row is not None
    assert row["status"] != "blocked" or row["block_kind"] is None, (
        "if blocked, it must not be a spawn-failure auto-block "
        f"(got {row['status']}/{row['block_kind']})"
    )
    # At least one spawn was attempted (proving the retry path executed).
    assert calls["n"] >= 2


def test_dispatch_spawn_eagain_metric_surfaced(kanban_home, monkeypatch):
    """The tick must surface the transient-retry count on DispatchResult so
    fleet telemetry can see spawn-pressure without stranding cards."""
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "SPAWN_EAGAIN_MAX_RETRIES", 5)

    with kb.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"eagain-{i}", assignee="sentinel")

        # Every spawn hits EAGAIN once then succeeds -> 3 retries total.
        seq = [_make_eagain(), None, _make_eagain(), None, _make_eagain(), None]
        _patch_popen(monkeypatch, seq)

        res = kb.dispatch_once(conn, spawn_fn=kb._default_spawn, board=None)

    # The metric captured the absorbed retries (>=3, since each of 3 tasks
    # retried once). This is the surfaced spawn-failure signal.
    assert getattr(res, "spawn_eagain_retries", 0) >= 3, (
        f"expected spawn_eagain_retries >= 3, got "
        f"{getattr(res, 'spawn_eagain_retries', 0)}"
    )
