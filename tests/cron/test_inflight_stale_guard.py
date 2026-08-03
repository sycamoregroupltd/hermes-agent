"""RED-first regression test for the cron in-flight claim leak (t_27b59583).

The leak
--------
``cron/scheduler.py`` tracks in-flight cron jobs in the module-level
``_running_job_ids`` set. ``_submit_with_guard`` adds a job id BEFORE the
future that owns its release exists: everything between the add and
``pool.submit`` — ``create_execution``, ``contextvars.copy_context()``, and
once running, the whole pre-future body of ``run_one_job`` (SessionDB
construction around L3150-3161, agent import/build, config load) — has no
``finally`` that discards the id. If any of it throws or hangs, the release
path in ``_run_and_release``'s ``finally`` never runs. Every later tick then
short-circuits with ``cron.scheduler: Job '<x>' already running — skipping``
with no ``last_error``, no failure counter, and no alert, until the whole
gateway process restarts (incident: jarvis ``board-pm-triage-*`` jobs,
2026-08-02).

This file is committed BEFORE the fix (red-first). Against the unfixed
scheduler these tests MUST FAIL: the stale id is never released, so the
primary assertion (``job_id not in get_running_job_ids()`` after a tick)
fails. The implementation task (t_3778a491) makes them pass by adding the
bounded stale-entry guard: on each tick, a claim older than
``max(2 * interval, floor)`` with no live future is force-released, logged
with a countable ``cron.inflight.forced_release`` signal, and surfaced via
``mark_job_run(..., success=False, error=...)`` as ``last_error``.

Design notes
------------
- The job store persists ``schedule`` as an already-parsed DICT
  (``{"kind": "interval", "minutes": N}``), not the string form
  ``parse_schedule`` consumes — the fixtures use the persisted dict shape.
- The guard's age bookkeeping (``_running_since`` / ``_running_futures`` /
  ``get_inflight_guard_stats``) does not exist yet on the unfixed scheduler.
  The helpers reference it defensively (``getattr``/``hasattr``) so the SAME
  file runs cleanly against both the red (unfixed) and the green (fixed)
  implementation; the leak simulation is identical either way — an id in
  ``_running_job_ids`` with no future ever installed.
"""

import time
from unittest.mock import patch

import pytest

import cron.scheduler as sched


@pytest.fixture(autouse=True)
def _clean_inflight():
    """Reset the in-memory running set so tests are isolated.

    Clears the guard bookkeeping defensively: on the unfixed scheduler only
    ``_running_job_ids`` exists; the age/future dicts and counters appear
    with the fix, and clearing them keeps the same file hermetic on both.
    """
    sched._running_job_ids.clear()
    for attr in ("_running_since", "_running_futures", "_forced_releases"):
        obj = getattr(sched, attr, None)
        if obj is not None:
            obj.clear()
    if hasattr(sched, "_forced_release_count"):
        sched._forced_release_count = 0
    yield
    sched._running_job_ids.clear()
    for attr in ("_running_since", "_running_futures", "_forced_releases"):
        obj = getattr(sched, attr, None)
        if obj is not None:
            obj.clear()


def _job(job_id="wedged", minutes=60):
    """Build a job row using the PERSISTED schedule dict shape."""
    return {
        "id": job_id,
        "name": f"board-pm-triage-{job_id}",
        "schedule": {
            "kind": "interval",
            "minutes": minutes,
            "display": f"every {minutes}m",
        },
    }


def _inject_stale_claim(job_id: str) -> None:
    """Simulate the leak exactly as the incident left it: the job id is in
    the running set but no future was ever installed, so the release path in
    the worker's ``finally`` can never run.

    On the unfixed scheduler ``_running_job_ids`` is the only bookkeeping, so
    this is precisely the shape of the real wedge. Once the bounded guard
    lands it also records an old start time — 6h ago, far past
    ``max(2 * 60m interval, 30m floor)`` — so the claim is past its
    allowance on the first sweep.
    """
    sched._running_job_ids.add(job_id)
    running_since = getattr(sched, "_running_since", None)
    if running_since is not None:
        running_since[job_id] = time.time() - 6 * 60 * 60  # 6h old


class TestStaleInflightLeak:
    def test_stale_claim_is_force_released_and_reported_after_tick(
        self, tmp_path, caplog
    ):
        """The regression: a leaked in-flight claim must be force-released by
        the next tick, surface as ``last_error``, and emit a countable
        signal — instead of silently skipping every fire until the gateway
        process restarts."""
        job = _job(job_id="board-pm-triage-wedged", minutes=60)
        job_id = job["id"]
        _inject_stale_claim(job_id)

        with caplog.at_level("WARNING"), \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path), \
             patch("cron.jobs.load_jobs", return_value=[job]), \
             patch.object(sched, "get_due_jobs", return_value=[]), \
             patch.object(sched, "mark_job_run") as mark:
            sched.tick(verbose=False)

        # RED: on the unfixed scheduler the tick never releases the id — it
        # short-circuits with "already running — skipping" and the id stays
        # in the set forever, so this assertion FAILS and proves the leak.
        assert job_id not in sched.get_running_job_ids()

        # GREEN (after the bounded guard): the release surfaces as a failure
        # on the job row instead of silence…
        assert mark.call_count == 1
        args = mark.call_args.args
        assert args[0] == job_id
        assert args[1] is False
        assert "in-flight" in args[2]

        # …and emits the countable forced-release signal (log + probe stats).
        assert any(
            "cron.inflight.forced_release" in r.message for r in caplog.records
        )
        stats = sched.get_inflight_guard_stats()
        assert stats["forced_releases"] == 1

    def test_young_inflight_claim_is_not_force_released(self, tmp_path):
        """Bound the guard: a claim younger than its allowance (and with no
        future) is left alone — the sweep must not double-dispatch healthy
        long-running jobs. Passes on both the red and the fixed code."""
        job = _job(job_id="young", minutes=60)
        job_id = job["id"]
        sched._running_job_ids.add(job_id)
        running_since = getattr(sched, "_running_since", None)
        if running_since is not None:
            running_since[job_id] = time.time() - 60  # 1 minute old

        with patch.object(sched, "_get_hermes_home", return_value=tmp_path), \
             patch("cron.jobs.load_jobs", return_value=[job]), \
             patch.object(sched, "get_due_jobs", return_value=[]), \
             patch.object(sched, "mark_job_run") as mark:
            sched.tick(verbose=False)

        assert job_id in sched.get_running_job_ids()
        mark.assert_not_called()
