"""Regression tests for the dead-store gate on ``hermes cron create`` (t_77316e9c).

``hermes cron create`` must refuse to register a job into a cron store whose
ticker is dead (missing or stale ``ticker_heartbeat``) unless ``--force`` is
passed. This closes the class at the registration boundary — the dead-store
invariant guard (t_4bedf8d5) becomes a safety net, not the primary control.
"""

import json
import time

import pytest

from hermes_cli import cron as cron_cli


def _heartbeat(store_dir, age_seconds):
    """Write a ticker_heartbeat ``age_seconds`` old into store_dir (or delete it)."""
    hb = store_dir / "ticker_heartbeat"
    if age_seconds is None:
        hb.unlink(missing_ok=True)
        return
    store_dir.mkdir(parents=True, exist_ok=True)
    hb.write_text(str(time.time() - age_seconds), encoding="utf-8")


def _args(**overrides):
    base = dict(
        schedule="every day",
        prompt="refresh docs",
        name=None,
        deliver=None,
        repeat=None,
        skill=None,
        skills=None,
        script=None,
        workdir=None,
        no_agent=False,
        model=None,
        model_provider=None,
        monitor_script=None,
        monitor_url=None,
        store=None,
        force=False,
    )
    base.update(overrides)
    return type("Args", (), base)()


class TestCronCreateDeadStoreGate:
    def test_fresh_store_allows_create(self, tmp_path, monkeypatch, capsys):
        store = tmp_path / "store"
        _heartbeat(store, age_seconds=10)  # 10s old — well within 900s threshold
        calls = []
        monkeypatch.setattr(
            cron_cli,
            "_cron_api",
            lambda **kw: calls.append(kw) or _ok_result(),
        )
        rc = cron_cli.cron_create(_args(store=str(store)))
        assert rc == 0
        assert len(calls) == 1
        assert "Created job" in capsys.readouterr().out

    def test_stale_heartbeat_refuses(self, tmp_path, monkeypatch, capsys):
        store = tmp_path / "store"
        _heartbeat(store, age_seconds=3600)  # 1h old > 900s
        calls = []
        monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: calls.append(kw) or _ok_result())
        rc = cron_cli.cron_create(_args(store=str(store)))
        out = capsys.readouterr().out
        assert rc == 1
        assert len(calls) == 0  # never reached the create call
        assert "Refusing to create job" in out
        assert "ticker_heartbeat" in out

    def test_missing_heartbeat_refuses(self, tmp_path, monkeypatch, capsys):
        store = tmp_path / "store"
        _heartbeat(store, age_seconds=None)  # missing file
        calls = []
        monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: calls.append(kw) or _ok_result())
        rc = cron_cli.cron_create(_args(store=str(store)))
        out = capsys.readouterr().out
        assert rc == 1
        assert len(calls) == 0
        assert "Refusing to create job" in out

    def test_force_overrides_dead_store(self, tmp_path, monkeypatch, capsys):
        store = tmp_path / "store"
        _heartbeat(store, age_seconds=3600)  # stale
        calls = []
        monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: calls.append(kw) or _ok_result())
        monkeypatch.setattr(
            "cron.jobs.update_job",
            lambda job_id, updates: {"id": job_id, **updates},
        )
        rc = cron_cli.cron_create(_args(store=str(store), force=True))
        out = capsys.readouterr().out
        assert rc == 0
        assert len(calls) == 1
        assert "Created job" in out
        assert "WARNING: created into dead store" in out

    def test_force_records_dead_store_override_in_metadata(self, tmp_path, monkeypatch, capsys):
        # With --force into a dead store, the created job's metadata is stamped
        # with dead_store_override. Exercised via the real create path.
        store = tmp_path / "store"
        _heartbeat(store, age_seconds=3600)
        rc = cron_cli.cron_create(
            _args(store=str(store), force=True, schedule="0 9 * * 1", prompt="maintenance")
        )
        assert rc == 0
        jobs_file = store / "jobs.json"
        assert jobs_file.exists()
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
        jobs = data.get("jobs", data)
        assert any(j.get("dead_store_override") for j in jobs)

    @pytest.mark.parametrize("failure", ["none", "exception"])
    def test_force_stamp_failure_is_visible_and_rolls_back(
        self, tmp_path, monkeypatch, capsys, failure
    ):
        # A force create must never report success without its audit stamp. If
        # stamping fails, the just-created record is removed and the CLI returns
        # a visible failure instead of leaving an ambiguous job behind.
        store = tmp_path / "store"
        _heartbeat(store, age_seconds=3600)
        monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: _ok_result())

        def broken_update(*args, **kwargs):
            if failure == "exception":
                raise RuntimeError("stamp unavailable")
            return None

        removed = []
        monkeypatch.setattr("cron.jobs.update_job", broken_update)
        monkeypatch.setattr(
            "cron.jobs.remove_job",
            lambda job_id: removed.append(job_id) or True,
        )

        rc = cron_cli.cron_create(_args(store=str(store), force=True))
        out = capsys.readouterr().out

        assert rc == 1
        assert "Could not record required dead_store_override metadata" in out
        assert "Created job" not in out
        assert removed == ["abc123def456"]

    def test_root_store_default_refuses_when_dead(self, tmp_path, monkeypatch, capsys):
        # No --store: the active profile's store (the resolved current store).
        # A dead active store must also refuse.
        cron_dir = tmp_path / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)
        (cron_dir / "ticker_heartbeat").unlink(missing_ok=True)  # dead
        monkeypatch.setattr("cron.jobs.CRON_DIR", cron_dir)
        monkeypatch.setattr("cron.jobs.JOBS_FILE", cron_dir / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", cron_dir / "output")
        calls = []
        monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: calls.append(kw) or _ok_result())
        rc = cron_cli.cron_create(_args())
        assert rc == 1
        assert len(calls) == 0
        assert "Refusing to create job" in capsys.readouterr().out

    def test_root_store_default_allows_when_fresh(self, tmp_path, monkeypatch, capsys):
        cron_dir = tmp_path / "cron"
        _heartbeat(cron_dir, age_seconds=5)
        monkeypatch.setattr("cron.jobs.CRON_DIR", cron_dir)
        monkeypatch.setattr("cron.jobs.JOBS_FILE", cron_dir / "jobs.json")
        monkeypatch.setattr("cron.jobs.OUTPUT_DIR", cron_dir / "output")
        calls = []
        monkeypatch.setattr(cron_cli, "_cron_api", lambda **kw: calls.append(kw) or _ok_result())
        rc = cron_cli.cron_create(_args())
        assert rc == 0
        assert len(calls) == 1


def _ok_result():
    return {
        "success": True,
        "job_id": "abc123def456",
        "name": "test",
        "schedule": "every 1440m",
        "next_run_at": "2099-01-01T00:00:00",
        "job": {"script": None, "no_agent": False, "workdir": None},
    }
