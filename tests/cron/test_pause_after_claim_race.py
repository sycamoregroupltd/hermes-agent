"""Claim-vs-pause race hardening (t_d0104339).

A job is claimed while enabled (``claim_job_for_fire`` CAS), then the operator
pauses it before the actual side effect starts (agent session / script pool
dispatch). Previously the in-flight execution still ran and ``mark_job_run``
stamped ``last_run_at`` AFTER ``paused_at`` — the cron-store-disabled-state-
watchdog divergence. The scheduler must re-check the CURRENT record under the
per-job fire fence at the side-effect boundary and abort as
``skipped-paused-after-claim``: no side effect, no run mark, no delivery.

These exercise the real store against a temp profile home (no mocks for the
store), per the E2E-over-mocks discipline for file-touching code.
"""
import threading

import pytest

from cron.scheduler_provider import CronScheduler


class _FireProvider(CronScheduler):
    """Minimal concrete provider exposing the split fire path."""

    @property
    def name(self) -> str:
        return "test-fire"

    def start(self, stop_event: threading.Event, **kwargs) -> None:
        return None


@pytest.fixture
def profile_store(tmp_path):
    """Temp profile home routed through the canonical cron store context."""
    import cron.jobs as jobs

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    with jobs.use_cron_store(profile_home):
        yield jobs, profile_home


def _run_job_recorder(run_calls):
    def _run_job(job, *, defer_agent_teardown=None, extra_prompt=None,
                 cancel_event=None):
        run_calls.append(job.get("id"))
        return True, "output", "final response", None
    return _run_job


def test_paused_after_claim_aborts_before_side_effect(profile_store, monkeypatch):
    """The core race: claim while enabled → pause → the claimed execution must
    NOT run, must NOT mark a run, must NOT deliver; the claim-time execution
    row is closed as skipped and last_run_at stays unchanged."""
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from cron.executions import list_executions

    run_calls = []
    mark_calls = []
    save_calls = []
    deliver_calls = []
    monkeypatch.setattr(scheduler, "run_job", _run_job_recorder(run_calls))
    monkeypatch.setattr(
        scheduler, "mark_job_run",
        lambda job_id, success, error=None, **kwargs: (
            mark_calls.append((job_id, success, error)) or True
        ),
    )
    monkeypatch.setattr(
        scheduler, "save_job_output",
        lambda job_id, output: save_calls.append(job_id) or "/tmp/out.md",
    )
    monkeypatch.setattr(
        scheduler, "_deliver_result",
        lambda *args, **kwargs: deliver_calls.append(args[0]) or None,
    )

    job = jobs.create_job(prompt="x", schedule="every 5m", name="race")
    jid = job["id"]
    claimed = jobs.claim_job_for_fire(jid, return_job=True)
    assert isinstance(claimed, dict), "claim must succeed while enabled"

    # Pause lands between claim and side-effect start (the 16-minute script
    # pool backlog from the incident, compressed to one call).
    jobs.pause_job(jid)
    assert jobs.get_job(jid)["enabled"] is False
    assert jobs.get_job(jid)["last_run_at"] is None

    result = scheduler.run_one_job(claimed)

    assert result is True
    assert run_calls == [], "side effect must not start for a paused job"
    assert mark_calls == [], "no run may be recorded for the aborted execution"
    assert save_calls == [], "no output may be saved for the aborted execution"
    assert deliver_calls == [], "no delivery may happen for the aborted execution"

    # last_run_at unchanged (never marked), and the claim-time execution row
    # is terminal with the skipped marker — no new execution row was created
    # and none was left 'running'.
    assert jobs.get_job(jid)["last_run_at"] is None
    execs = list_executions(job_id=jid)
    assert len(execs) == 1, "aborted execution must not create extra rows"
    assert execs[0]["status"] == "failed"
    assert "skipped-paused-after-claim" in execs[0]["error"]
    assert all(e["status"] != "running" for e in execs)


def test_enabled_after_claim_still_runs(profile_store, monkeypatch):
    """Regression: a normally-enabled claimed job still executes and marks."""
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from cron.executions import list_executions

    run_calls = []
    deliver_calls = []
    monkeypatch.setattr(scheduler, "run_job", _run_job_recorder(run_calls))
    monkeypatch.setattr(
        scheduler, "_deliver_result",
        lambda *args, **kwargs: deliver_calls.append(args[0]) or None,
    )

    job = jobs.create_job(prompt="x", schedule="every 5m", name="healthy")
    jid = job["id"]
    claimed = jobs.claim_job_for_fire(jid, return_job=True)
    assert isinstance(claimed, dict)

    result = scheduler.run_one_job(claimed)

    assert result is True
    assert run_calls == [jid], "enabled job must run"
    assert jobs.get_job(jid)["last_run_at"] is not None, "run must be marked"
    assert jobs.get_job(jid)["fire_claim"] is None, "claim cleared on completion"
    execs = list_executions(job_id=jid)
    assert len(execs) == 1
    assert execs[0]["status"] == "completed"


def test_fire_due_path_skips_paused_after_claim(profile_store, monkeypatch):
    """The external provider split-fire path (claim_fire → fire_claimed) gets
    the same guarantee: pause between claim and run aborts the execution."""
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from cron.executions import list_executions

    run_calls = []
    monkeypatch.setattr(scheduler, "run_job", _run_job_recorder(run_calls))

    job = jobs.create_job(prompt="x", schedule="every 5m", name="provider-race")
    jid = job["id"]
    provider = _FireProvider()

    claimed = provider.claim_fire(jid)
    assert isinstance(claimed, dict), "claim must succeed while enabled"
    execution_id = claimed["execution_id"]

    jobs.pause_job(jid)
    assert jobs.get_job(jid)["last_run_at"] is None

    assert provider.fire_claimed(claimed) is True

    assert run_calls == [], "provider fire must not run a paused job"
    assert jobs.get_job(jid)["last_run_at"] is None
    execs = list_executions(job_id=jid)
    assert len(execs) == 1
    assert execs[0]["id"] == execution_id, "claim-time row reused, not duplicated"
    assert execs[0]["status"] == "failed"
    assert "skipped-paused-after-claim" in execs[0]["error"]


def test_disabled_without_claim_also_aborts(profile_store, monkeypatch):
    """Defense in depth: even a direct fire with no fire_claim must not start
    the side effect of a job that is paused/disabled in the CURRENT store."""
    import cron.jobs as jobs
    import cron.scheduler as scheduler

    run_calls = []
    monkeypatch.setattr(scheduler, "run_job", _run_job_recorder(run_calls))

    job = jobs.create_job(prompt="x", schedule="every 5m", name="direct-pause")
    jid = job["id"]
    jobs.pause_job(jid)

    # No claim at all — the snapshot is the (now stale) enabled record.
    stale_snapshot = {"id": jid, "name": "direct-pause", "prompt": "x"}

    assert scheduler.run_one_job(stale_snapshot) is True
    assert run_calls == []
    assert jobs.get_job(jid)["last_run_at"] is None
