#!/usr/bin/env python3
"""
cron_ticker_invariant_guard.py — every-30m no_agent invariant guard (t_4bedf8d5).

WHY: the fleet's cron tier had 26+ enabled jobs registered in stores whose
gateway/ticker never ticks (devops ticker frozen 22d, elon/eval-runner/guardian/
nse/sie/test-engineer/tdo/trr/ttf had NO heartbeat at all). `hermes cron list`
rendered them [active] with no Last-run line — the CLI actively concealed the
outage. This guard closes the CLASS: any enabled job in a store whose
ticker_heartbeat is missing or >900s stale gets

  1) a RED alert line (no_agent stdout -> delivered to discord:#critical-alerts)
  2) auto-disabled with paused_reason set, so cron list renders [paused] and
     stops claiming the job is live.

SILENT WHEN CLEAN (watchdog pattern: empty stdout = no delivery).

Safe-by-construction:
  - READ-ONLY on stores whose heartbeat is FRESH (never touches ticking stores).
  - Only writes to stores whose heartbeat is missing/stale (dead stores have no
    concurrent ticker writer, so the atomic replace is race-free in practice;
    we still take the store's .jobs.lock flock when present).
  - Never disables jobs in this guard's own store (jarvis heartbeat is fresh).
  - Idempotent: already-paused jobs are skipped; re-runs stay silent.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REAL_HERMES_HOME = Path(os.environ.get("HERMES_REAL_HOME", "/home/frank/.hermes")).expanduser()
if not (REAL_HERMES_HOME / "profiles").exists() and (REAL_HERMES_HOME / ".hermes" / "profiles").exists():
    REAL_HERMES_HOME = REAL_HERMES_HOME / ".hermes"

PROFILES_DIR = REAL_HERMES_HOME / "profiles"
ROOT_STORE = REAL_HERMES_HOME / "cron" / "jobs.json"

STALE_SECONDS = int(os.environ.get("TICKER_STALE_SECONDS", "900"))  # 15m
# Only act on stores with enabled jobs. Stores with 0 enabled jobs are healthy
# even if the heartbeat is old (e.g. root store with everything disabled).


def _epoch_file_age(path: Path) -> float | None:
    """Seconds since the ticker last looped, or None if missing/unreadable."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return max(0.0, time.time() - float(raw))
    except Exception:
        return None


def store_paths() -> list[Path]:
    paths: list[Path] = []
    if PROFILES_DIR.exists():
        paths.extend(sorted(PROFILES_DIR.glob("*/cron/jobs.json")))
    if ROOT_STORE.exists():
        paths.append(ROOT_STORE)
    return paths


def _load_jobs(store: Path) -> tuple[dict | list, list[dict]]:
    """Return (raw_container, list-of-job-dicts). Raises on parse failure."""
    raw = json.loads(store.read_text(encoding="utf-8"))
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    return raw, jobs


def _store_is_dead(store: Path) -> tuple[bool, str]:
    """(is_dead, reason). A store is dead if its ticker heartbeat is missing
    or older than STALE_SECONDS. The root store's heartbeat file lives beside
    it (cron/ticker_heartbeat)."""
    hb = store.parent / "ticker_heartbeat"
    age = _epoch_file_age(hb)
    if age is None:
        return True, "ticker_heartbeat missing/unreadable"
    if age > STALE_SECONDS:
        return True, f"ticker_heartbeat {int(age)}s stale (>{STALE_SECONDS}s)"
    return False, ""


def _atomic_write(store: Path, raw) -> None:
    """Write the store container back atomically with a dated backup."""
    backup = store.with_name(f"jobs.json.bak-guard-{int(time.time())}")
    try:
        store.rename(backup)
    except OSError:
        pass  # backup best-effort; atomic replace below still protects
    fd, tmp = tempfile.mkstemp(dir=str(store.parent), prefix=".jobs_guard_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, default=str)
        os.replace(tmp, store)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _flock(store: Path):
    """Best-effort advisory lock on the store's .jobs.lock (fcntl only)."""
    try:
        import fcntl

        lock_path = store.parent / ".jobs.lock"
        fh = open(lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except Exception:
        return None


def main() -> int:
    alerts: list[str] = []
    for store in store_paths():
        try:
            raw, jobs = _load_jobs(store)
        except Exception as exc:
            alerts.append(f"GUARD-ERROR {store}: unreadable store ({exc})")
            continue
        enabled = [j for j in jobs if isinstance(j, dict) and j.get("enabled", False)]
        if not enabled:
            continue  # healthy store (or already cleaned): nothing to do

        dead, reason = _store_is_dead(store)
        if not dead:
            continue  # ticking store: never touch

        store_label = str(store.relative_to(REAL_HERMES_HOME)) if store.is_relative_to(REAL_HERMES_HOME) else str(store)
        changed = False
        for job in enabled:
            jid = job.get("id", "?")
            name = job.get("name", "(unnamed)")
            job["enabled"] = False
            job["state"] = "paused"
            job["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "+00:00"
            job["paused_reason"] = (
                f"dead-store-invariant-guard: {store_label} {reason} "
                f"[t_4bedf8d5]"
            )
            changed = True
            alerts.append(
                f"🔴 DEAD-STORE-INVARIANT: {store_label} {reason} -> "
                f"auto-disabled job '{name}' ({jid}) [t_4bedf8d5]"
            )
        if changed:
            lock = _flock(store)
            try:
                _atomic_write(store, raw)
            finally:
                if lock is not None:
                    try:
                        lock.close()
                    except Exception:
                        pass

    if alerts:
        print("\n".join(alerts))
    # Exit 0 on any SUCCESSFUL run: for a no_agent watchdog, non-empty stdout is
    # the alert channel (delivered verbatim to discord:#critical-alerts) and
    # empty stdout is the silent/clean signal. A non-zero exit is reserved for
    # an actual script failure (unhandled exception / missing store), which the
    # cron scheduler escalates as a broken-watchdog alert — marking a healthy
    # reporting run as "failed" would trip fleet failed-run monitors during
    # exactly the outage this guard exists to announce.
    return 0


if __name__ == "__main__":
    sys.exit(main())
