#!/usr/bin/env python3
"""Fixtures for fleet_knowledge_catalog_regen_watchdog.py (t_e445eddb).

Exercises the watchdog against an isolated temp cron store; never mutates the
live jarvis profile unless the caller explicitly points env vars at it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path("/home/frank/.hermes")
HERMES_AGENT = ROOT / "hermes-agent"
WATCHDOG = ROOT / "scripts" / "fleet_knowledge_catalog_regen_watchdog.py"
TARGET_ID = "3ddf2469949e"
TARGET_NAME = "fleet-knowledge-catalog-regen"
SENTINEL_ID = "fixture-sentinel"


def _read_jobs(home: Path) -> list[dict]:
    data = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
    return data.get("jobs", [])


def _base_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_PROFILE": "jarvis-fixture",
            "HERMES_CATALOG_REGEN_WATCHDOG_HOME": str(home),
            "HERMES_CATALOG_REGEN_WATCHDOG_LOG": str(home / "watchdog.log"),
            "PYTHONPATH": f"{HERMES_AGENT}:{env.get('PYTHONPATH', '')}",
        }
    )
    return env


def run_missing_heal_fixture() -> None:
    with tempfile.TemporaryDirectory(prefix="watchdog-missing-") as td:
        home = Path(td)
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text('{"jobs": []}\n', encoding="utf-8")
        proc = subprocess.run([sys.executable, str(WATCHDOG)], env=_base_env(home), text=True, capture_output=True)
        jobs = _read_jobs(home)
        assert proc.returncode == 1, proc
        assert any(j.get("id") == TARGET_ID and j.get("name") == TARGET_NAME for j in jobs), jobs
        assert "REPAIR" in proc.stdout, proc.stdout
        print("PASS missing-heal fixture: target recreated and repair alert emitted")


def run_idempotent_fixture() -> None:
    with tempfile.TemporaryDirectory(prefix="watchdog-idempotent-") as td:
        home = Path(td)
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": TARGET_ID, "name": TARGET_NAME, "enabled": True}]}) + "\n",
            encoding="utf-8",
        )
        proc = subprocess.run([sys.executable, str(WATCHDOG)], env=_base_env(home), text=True, capture_output=True)
        jobs = _read_jobs(home)
        assert proc.returncode == 0, proc
        assert sum(1 for j in jobs if j.get("name") == TARGET_NAME) == 1, jobs
        assert proc.stdout == "", proc.stdout
        print("PASS idempotent fixture: target present => silent no-op")


def run_lost_update_fixture() -> None:
    """Prove watchdog waits for the canonical jobs lock and preserves peer writes.

    The peer process holds cron/.jobs.lock, reads the empty store, appends a
    sentinel, sleeps while holding the lock, then atomically saves. The watchdog
    starts while that lock is held. If the watchdog used an unlocked read -> bare
    write path, the final store could lose the sentinel. The expected final
    state contains both the sentinel and the restored catalog job.
    """
    with tempfile.TemporaryDirectory(prefix="watchdog-lock-") as td:
        home = Path(td)
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text('{"jobs": []}\n', encoding="utf-8")
        env = _base_env(home)
        peer_code = textwrap.dedent(
            f"""
            import json, sys, time
            from pathlib import Path
            sys.path.insert(0, {str(HERMES_AGENT)!r})
            from cron.jobs import _jobs_lock, _save_jobs_unlocked, load_jobs, use_cron_store
            home = Path({str(home)!r})
            with use_cron_store(home), _jobs_lock():
                jobs = load_jobs()
                jobs.append({{"id": {SENTINEL_ID!r}, "name": "fixture-peer-write", "enabled": True}})
                time.sleep(1.0)
                _save_jobs_unlocked(jobs)
            """
        )
        peer = subprocess.Popen([sys.executable, "-c", peer_code], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        watchdog = subprocess.run([sys.executable, str(WATCHDOG)], env=env, text=True, capture_output=True, timeout=10)
        peer_out, peer_err = peer.communicate(timeout=10)
        assert peer.returncode == 0, (peer.returncode, peer_out, peer_err)
        assert watchdog.returncode == 1, watchdog
        ids = {j.get("id") for j in _read_jobs(home)}
        assert TARGET_ID in ids, ids
        assert SENTINEL_ID in ids, ids
        print("PASS lost-update fixture: canonical lock/atomic save preserved concurrent sentinel + target")


def main() -> int:
    run_missing_heal_fixture()
    run_idempotent_fixture()
    run_lost_update_fixture()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
