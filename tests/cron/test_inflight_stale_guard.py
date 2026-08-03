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

IMPORTANT (review round 2): the job store persists ``schedule`` as an
already-parsed DICT (``{"kind": "interval", "minutes": N}`` /
``{"kind": "cron", "expr": "..."}``), not the string form ``parse_schedule``
consumes. All fixtures here use the persisted dict shape so the tests prove
the production path, not a synthetic string shape.
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


def _job(job_id="wedged", minutes=60, kind="interval", cron_expr=None,
         repeat=None):
    """Build a job row using the PERSISTED schedule dict shape."""
    if kind == "interval":
        schedule = {"kind": "interval", "minutes": minutes,
                    "display": f"every {minutes}m"}
    elif kind == "cron":
        expr = cron_expr or "0 9 * * 1"
        schedule = {"kind": "cron", "expr": expr, "display": expr}
    else:
        schedule = {"kind": "once", "run_at": "2030-01-01T00:00:00",
                    "display": "once at 2030-01-01 00:00"}
    job = {"id": job_id, "name": f"board-pm-triage-{job_id}",
           "schedule": schedule}
    if repeat is not None:
        job["repeat"] = repeat
    return job


# Real persisted row shapes copied verbatim from
# /home/frank/.hermes/profiles/jarvis/cron/jobs.json (2026-08-03) — the
# review found the first attempt's string fixture proved nothing about the
# store, so these pin the actual store shape.
REAL_INTERVAL_ROW = {"kind": "interval", "minutes": 4320,
                     "display": "every 4320m"}          # guide-curator
REAL_CRON_ROW = {"kind": "cron", "expr": "*/15 * * * *",
                 "display": "*/15 * * * *"}             # dgx-jarvis-health-canary
REAL_WEEKLY_CRON_ROW = {"kind": "cron", "expr": "0 9 * * 1",
                        "display": "0 9 * * 1"}         # weekly-security-audit


class TestJobIntervalMinutes:
    """Blocker-1 fix: interval must come from the PERSISTED dict shape."""

    def test_reads_persisted_interval_dict(self):
        job = {"id": "x", "schedule": dict(REAL_INTERVAL_ROW)}
        assert sched._job_interval_minutes(job) == 4320.0

    def test_reads_persisted_cron_dict(self):
        job = {"id": "x", "schedule": dict(REAL_CRON_ROW)}
        # */15 every 15 minutes → cadence 15m.
        assert sched._job_interval_minutes(job) == 15.0

    def test_reads_persisted_weekly_cron_dict(self):
        job = {"id": "x", "schedule": dict(REAL_WEEKLY_CRON_ROW)}
        # 0 9 * * 1 fires weekly → cadence 7*24*60 = 10080m.
        assert sched._job_interval_minutes(job) == 7 * 24 * 60

    def test_string_fallback_still_works(self):
        # Defensive fallback for programmatic callers.
        job = {"id": "x", "schedule": "every 60m"}
        assert sched._job_interval_minutes(job) == 60.0

    def test_oneshot_has_no_interval(self):
        job = _job(kind="once")
        assert sched._job_interval_minutes(job) is None

    def test_garbage_returns_none(self):
        job = {"id": "x", "schedule": {"kind": "bogus"}}
        assert sched._job_interval_minutes(job) is None


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
        job = _job(minutes=360)
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 4 * 60 * 60  # 4h < 12h

        with patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == []
        assert job["id"] in sched.get_running_job_ids()

    def test_allowance_honors_persisted_4320m_row(self, tmp_path):
        """The REAL guide-curator row (4320m) gets a 144h allowance, not the
        30m floor — the Blocker-1 regression against the live store shape."""
        job = _job(job_id="guide-curator", minutes=4320)
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 4 * 60 * 60  # 4h ≪ 144h

        with patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == []
        assert job["id"] in sched.get_running_job_ids()

    def test_cron_allowance_not_clipped_to_floor(self, tmp_path):
        """A weekly cron job (cadence 10080m) is not clipped at 30m: a 24h
        claim is still healthy (allowance 20160m)."""
        job = _job(job_id="weekly", kind="cron", cron_expr="0 9 * * 1")
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 24 * 60 * 60

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

    def test_pending_sentinel_released_when_submit_hung(self, tmp_path):
        """MINOR 1: a claim whose submit path hung stays _FUTURE_PENDING past
        its allowance (the SessionDB-init wedge) and must be released."""
        job = _job()
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 5 * 60 * 60
        sched._running_futures[job["id"]] = sched._FUTURE_PENDING

        with patch.object(sched, "mark_job_run") as mark, \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == [job["id"]]
        assert mark.call_count == 1

    def test_pending_sentinel_young_claim_is_not_released(self, tmp_path):
        """MINOR 1: a young pending claim (submit still in flight) is safe."""
        job = _job()
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 60  # 1 minute
        sched._running_futures[job["id"]] = sched._FUTURE_PENDING

        with patch.object(sched, "mark_job_run") as mark, \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == []
        assert job["id"] in sched.get_running_job_ids()
        mark.assert_not_called()

    def test_finite_repeat_job_released_without_mark_job_run(self, tmp_path):
        """MINOR 2: a forced release must not consume a finite repeat budget
        or auto-delete the row; the claim is released, the row untouched."""
        job = _job(repeat={"times": 1, "completed": 0})
        sched._running_job_ids.add(job["id"])
        sched._running_since[job["id"]] = time.time() - 5 * 60 * 60

        with patch.object(sched, "mark_job_run") as mark, \
             patch.object(sched, "_get_hermes_home", return_value=tmp_path):
            assert sched.sweep_stale_inflight([job]) == [job["id"]]
        assert job["id"] not in sched.get_running_job_ids()
        mark.assert_not_called()
        assert sched.get_inflight_guard_stats()["forced_releases"] == 1

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
        job = dict(_job(), enabled=True, next_run_at="2020-01-01T00:00:00",
                   deliver="local")
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
