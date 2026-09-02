"""Integration tests for the Jarvis loop-registry producer guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_REGISTRY_ROW = """loops:
- id: test-jarvis-loop
  name: test-jarvis-loop
  kind: cron
  status: active
  owner: jarvis
  project: control-plane
  trigger: every 5m
  oracle: isolated producer guard test
  budget: one bounded test pass
  consumer: isolated test assertion
  retirement: retire after the test
  store: /tmp/test-jarvis-loop
  job_id: test-jarvis-loop
  skills:
  - fleet-loop-registry
"""


@pytest.fixture
def isolated_jarvis(tmp_path, monkeypatch):
    """Use the real cron persistence path under a disposable Jarvis profile."""
    root = tmp_path / "hermes-root"
    home = root / "profiles" / "jarvis"
    home.mkdir(parents=True)
    registry = root / "loop-registry" / "registry.yaml"
    registry.parent.mkdir()
    registry.write_text(_REGISTRY_ROW, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from cron import jobs

    return jobs, home, registry


def test_create_rejects_unregistered_enabled_job_before_store_write(isolated_jarvis):
    jobs, home, _registry = isolated_jarvis

    with pytest.raises(ValueError, match="not registered"):
        jobs.create_job(
            job_id="not-registered",
            prompt="should not persist",
            schedule="every 5m",
        )

    jobs_path = home / "cron" / "jobs.json"
    assert not jobs_path.exists()


def test_real_create_and_enabled_update_require_active_registry_row(isolated_jarvis):
    jobs, home, registry = isolated_jarvis

    created = jobs.create_job(
        job_id="test-jarvis-loop",
        prompt="registered create",
        schedule="every 5m",
    )
    assert created["id"] == "test-jarvis-loop"

    updated = jobs.update_job(created["id"], {"name": "updated", "enabled": True})
    assert updated["name"] == "updated"

    registry.write_text(_REGISTRY_ROW.replace("status: active", "status: paused"), encoding="utf-8")
    with pytest.raises(ValueError, match="only active rows may be enabled"):
        jobs.update_job(created["id"], {"enabled": True})

    stored = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
    persisted = stored["jobs"][0]
    assert persisted["name"] == "updated"
    assert persisted["enabled"] is True


def test_enabled_update_rejects_row_removed_after_job_was_paused(isolated_jarvis):
    jobs, home, registry = isolated_jarvis

    created = jobs.create_job(
        job_id="test-jarvis-loop",
        prompt="registered create",
        schedule="every 5m",
    )
    jobs.update_job(created["id"], {"enabled": False, "state": "paused"})
    registry.write_text("loops: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not registered"):
        jobs.update_job(created["id"], {"enabled": True, "state": "scheduled"})

    stored = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert stored["jobs"][0]["enabled"] is False
