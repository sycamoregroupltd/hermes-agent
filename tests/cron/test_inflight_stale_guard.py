"""Regression tests for the cron in-flight claim leak (t_560fc38a).

The bug: ``_submit_with_guard`` adds a job id to ``_running_job_ids`` BEFORE
the future exists. If anything between the add and the future hangs or dies
(the documented case is a wedged ``SessionDB.__init__``), the release path in
``_run_and_release``'s ``finally`` never runs, and every later tick
short-circuits with "already running — skipping" — silently, with no
``last_error`` and no failure counter — until the whole gateway restarts.

These tests pin the bounded guard: a claim older than its allowance with no
live future is force-released, logged, counted, and surfaced via
``mark_job_run(..., success=False, error=...)``.
"""

import time
from unittest.mock import patch

import pytest

import cron.scheduler as sched


@pytest.fixture(autouse=True)
def _clean_inflight():
    sched._running_job_ids.clear()
    sched._running_since.clear()
    sched._running_futures.clear()
    sched._forced_releases.clear()
    sched._forced_release_count = 0
    yield
    sched._running_job_ids.clear()
    sched._running_since.clear()
    sched._running_futures.clear()


def _job(job_id="wedged", schedule="every 60m"):
    return {"id": job_id, "name": f"board-pm-triage-{job_id}", "schedule": schedule}


class TestStaleInflightSweep:
    def test_add_without_release_is_force_released_and_reported(self, tmp_path):
        job = _job()
        # Simulate the leak: claim taken, no future ever installed.
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 3 * 60 * 60  # 3h ago

        with patch.object(sched, "mark_job_run") as mark, \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            released = sched.sweep_stale_inflight([job])

        assert released == [job["id"]]
        assert job["id"] not in sched.get_running_job_ids()
        # Surfaces as a failure on the job row, not silence.
        assert mark.call_count == 1
        args = mark.call_args.args
        assert args[0] == job["id"] and args[1] is False
        assert "in-flight" in args[2]

        stats = sched.get_inflight_guard_stats()
        assert stats["forced_releases"] == 1
        assert stats["recent_forced_releases"][-1]["job_id"] == job["id"]

        # Countable, probe-visible artifact on disk.
        jsonl = tmp_path / "cron" / "inflight_forced_releases.jsonl"
        assert jsonl.exists() and job["id"] in jsonl.read_text()

    def test_young_claim_is_not_released(self, tmp_path):
        job = _job()
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 60  # 1 minute

        with patch.object(sched, "mark_job_run") as mark, \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == []
        assert job["id"] in sched.get_running_job_ids()
        mark.assert_not_called()

    def test_allowance_is_at_least_two_intervals(self, tmp_path):
        """A slow-but-healthy 6h job is not clipped by the 30m floor."""
        job = _job(schedule="every 360m")
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 4 * 60 * 60  # 4h < 12h

        with patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == []
        assert job["id"] in sched.get_running_job_ids()

    def test_live_future_is_never_released(self, tmp_path):
        import concurrent.futures

        job = _job()
        fut: concurrent.futures.Future = concurrent.futures.Future()
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 10 * 60 * 60
        sched._running_futures[job["id"]] = fut

        with patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == []
        assert job["id"] in sched.get_running_job_ids()
        fut.set_result(True)

        # Once the future is done but the id somehow survived, it IS stale.
        with patch.object(sched, "mark_job_run"), \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == [job["id"]]

    def test_claim_without_timestamp_is_adopted_then_swept(self, tmp_path):
        """An id injected with no recorded start (pre-guard claim) must not be
        released immediately, but must become sweepable."""
        job = _job()
        sched._running_job_ids.add(job["id"])

        with patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == []
            assert job["id"] in sched._running_since
            sched._running_since[job["id"]] -= 5 * 60 * 60
            with patch.object(sched, "mark_job_run"):
                assert sched.sweep_stale_inflight([job]) == [job["id"]]

    def test_forced_release_logs_a_warning(self, tmp_path, caplog):
        job = _job()
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 5 * 60 * 60

        with caplog.at_level("WARNING"), \
             patch.object(sched, "mark_job_run"), \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            sched.sweep_stale_inflight([job])

        assert any("cron.inflight.forced_release" in r.message for r in caplog.records)


class TestWedgedJobRefiresWithoutRestart:
    def test_tick_sweeps_then_dispatches_the_previously_wedged_job(self, tmp_path):
        """End-to-end symptom: before the fix, tick() returned 0 forever."""
        job = dict(_job(), enabled=True, next_run_at="2020-01-01T00:00:00", deliver="local")
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 6 * 60 * 60

        with patch.object(sched, "_get_hermes_home", return_value=tmp_path), \
             patch.object(sched, "get_due_jobs", return_value=[job]), \
             patch("cron.jobs.load_jobs", return_value=[job]), \
             patch.object(sched, "advance_next_run"), \
             patch.object(sched, "mark_job_run"), \
             patch.object(sched, "create_execution", return_value={"id": "exec-1"}), \
             patch.object(sched, "finish_execution"), \
             patch.object(sched, "run_one_job", return_value=True):
            n = sched.tick(verbose=False)

        assert n == 1, "wedged job must fire again without a gateway restart"
        assert job["id"] not in sched.get_running_job_ids()
        assert sched.get_inflight_guard_stats()["forced_releases"] == 1
