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

AUTO RE-ARM (t_a8fdd2db): the guard is TWO-WAY. When a store it previously
paused (paused_reason = "dead-store-invariant-guard: ... [t_4bedf8d5]") has its
ticker_heartbeat return to FRESH (<=STALE_SECONDS), the guard re-arms every
job it owns (that paused_reason marker) back to enabled/scheduled — so a brief
heartbeat blip (e.g. a gateway restart) no longer silently kills the whole cron
tier with no recovery path (the 2026-08-29 incident: 170/173 jarvis jobs stayed
paused ~9h after a transient restart). Re-arm is:

  - CONFIRMED-FRESH ONLY: we re-arm a store's guard-paused jobs ONLY when the
    heartbeat is verified fresh at the time of the write (checked again under
    the lock). We NEVER re-arm a store whose heartbeat is still stale.
  - GUARD-MARKER ONLY: we only re-arm jobs carrying OUR paused_reason marker
    ("dead-store-invariant-guard:"). Intentional pauses (CONDENSE, retired,
    operator manual) are never touched.
  - SERIALIZED: re-arm writes to a FRESH store while its ticker is live, so we
    take a blocking flock on .jobs.lock (mirroring the scheduler) and re-read
    the store under the lock before writing, to avoid clobbering a concurrent
    ticker update (next_run_at/last_run_at).
  - IDEMPOTENT + evidence-logged: each re-armed job emits a GREEN alert line
    naming the recovery-eligible set and heartbeat age; already-enabled jobs
    are skipped.

SILENT WHEN CLEAN (watchdog pattern: empty stdout = no delivery).

Safe-by-construction:
  - A FRESH store is never touched for pausing (only re-armed from guard-paused).
  - Only writes to stores whose heartbeat is missing/stale for pausing (dead
    stores have no concurrent ticker writer) OR to a fresh store under a
    blocking flock for re-arming jobs it previously paused.
  - Never disables jobs in this guard's own store when that store's heartbeat
    is fresh (it re-arms only; it does not pause a live store).
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
# Bounded wait on the store's .jobs.lock for re-arm writes to a fresh store.
# The scheduler's own flock has a 30s bound; mirror that so a wedged sibling
# lockholder cannot hang this guard forever (fail-safe: skip & alert instead).
LOCK_TIMEOUT_SECONDS = float(os.environ.get("JOBS_LOCK_TIMEOUT_SECONDS", "25"))

GUARD_MARKER = "dead-store-invariant-guard:"


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
        seen_real: set[str] = set()
        for p in sorted(PROFILES_DIR.glob("*/cron/jobs.json")):
            # Dedupe symlinked profile stores (e.g. sycode-trading -> sycode-trading-pm)
            # so one physical jobs.json is guarded/written exactly once, under its
            # real store path (mirrors dgx_cron_health_canary.iter_profile_jobs).
            real = str(p.resolve())
            if real in seen_real:
                continue
            seen_real.add(real)
            paths.append(p)
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


def _is_guard_paused(job: dict) -> bool:
    """True if this job was paused BY THIS GUARD (our paused_reason marker)."""
    return str(job.get("paused_reason", "")).startswith(GUARD_MARKER)


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
    """Best-effort NON-blocking advisory lock on the store's .jobs.lock.

    Suitable for PAUSING jobs in a DEAD store (no concurrent ticker writer).
    Returns the open file handle when acquired, else None (caller still writes
    — a dead store has no live writer to collide with)."""
    fh = None
    try:
        import fcntl

        lock_path = store.parent / ".jobs.lock"
        fh = open(lock_path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return None


def _blocking_flock(store: Path):
    """Blocking advisory lock on .jobs.lock with a bounded wait (fcntl only).

    Required for RE-ARMING jobs in a FRESH store whose ticker is live and
    serializes load-modify-save via the same .jobs.lock flock. Returns the open
    handle on acquisition, or None on timeout/unsupported — the caller MUST
    skip the write (fail-safe) when None, because a fresh store's ticker may be
    mid-write and an unserialized full-container replace would clobber it."""
    fh = None
    try:
        import fcntl

        lock_path = store.parent / ".jobs.lock"
        fh = open(lock_path, "a+")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    try:
                        fh.close()
                    except Exception:
                        pass
                    return None
                time.sleep(0.2)
    except Exception:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return None


def _store_relative(store: Path, real_home: Path) -> str:
    try:
        return str(store.relative_to(real_home))
    except ValueError:
        return str(store)


def _rearm_store(store: Path, real_home: Path) -> tuple[list[str], list[str]]:
    """Re-arm guard-paused jobs in a FRESH store. Takes the blocking flock,
    re-reads under the lock, re-verifies the heartbeat is fresh, and re-arms
    ONLY the jobs carrying this guard's paused_reason marker.

    Returns (rearmed_alerts, guard_errors). Empty alerts => nothing re-armed."""
    alive, reason = _store_is_dead(store)
    if alive:
        # Heartbeat went stale again between the outer scan and now — fail-safe,
        # never re-arm a store whose heartbeat is not confirmed fresh.
        return [], []
    lock = _blocking_flock(store)
    if lock is None:
        return [], [
            f"🔴 GUARD-ERROR {_store_relative(store, real_home)}: "
            f"could not acquire .jobs.lock within {LOCK_TIMEOUT_SECONDS}s to "
            f"re-arm (fresh store ticker may be busy); skipped this cycle "
            f"[t_a8fdd2db]"
        ]
    try:
        # Re-verify freshness under the lock (heartbeat may have aged while waiting).
        alive, reason = _store_is_dead(store)
        if alive:
            return [], []
        try:
            raw, jobs = _load_jobs(store)
        except Exception as exc:
            return [], [f"🔴 GUARD-ERROR {_store_relative(store, real_home)}: unreadable store on re-arm ({exc})"]
        rearmed: list[tuple[str, str]] = []  # (name, id)
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if not _is_guard_paused(job):
                continue
            if job.get("enabled", False):
                continue  # already enabled; nothing to do
            job["enabled"] = True
            job["state"] = "scheduled"
            job["paused_at"] = None
            job["paused_reason"] = None
            # next_run_at=None lets the scheduler's due-scan recompute the next
            # future occurrence (verified in get_due_jobs: no burst-fire, picks
            # up at the next scheduled tick). Avoids bundling croniter here.
            job["next_run_at"] = None
            rearmed.append((job.get("name", "(unnamed)"), job.get("id", "?")))
        if not rearmed:
            return [], []
        _atomic_write(store, raw)
        age = _epoch_file_age(store.parent / "ticker_heartbeat")
        age_s = "?" if age is None else f"{int(age)}s"
        alerts = [
            f"🟢 GUARD RE-ARM (recovery): {_store_relative(store, real_home)} "
            f"heartbeat fresh ({age_s}) -> re-armed job '{name}' ({jid}) "
            f"[t_a8fdd2db]"
            for name, jid in rearmed
        ]
        return alerts, []
    finally:
        try:
            lock.close()
        except Exception:
            pass


def main() -> int:
    alerts: list[str] = []
    for store in store_paths():
        try:
            raw, jobs = _load_jobs(store)
        except Exception as exc:
            alerts.append(f"GUARD-ERROR {store}: unreadable store ({exc})")
            continue
        enabled = [
            j
            for j in jobs
            if isinstance(j, dict)
            and j.get("enabled", False)
            and not _is_guard_paused(j)
        ]
        guard_paused = [j for j in jobs if isinstance(j, dict) and _is_guard_paused(j)]
        if not enabled and not guard_paused:
            continue  # healthy store (or already cleaned): nothing to do

        dead, reason = _store_is_dead(store)
        if not dead and guard_paused:
            # Store recovered: re-arm jobs we previously paused. Also catches a
            # fresh store that somehow still carries OUR pause marker (e.g. an
            # operator resumed others but left the marker) — the marker is the
            # authoritative "paused-by-guard" signal, so we own those.
            rearmed, errors = _rearm_store(store, REAL_HERMES_HOME)
            alerts.extend(rearmed)
            alerts.extend(errors)
            # If re-arm restored every job, there is nothing left to pause.
            # Re-derive: jobs are live now, but never pause a FRESH store.
            continue

        if not dead:
            continue  # ticking store, nothing guard-paused: never touch

        store_label = _store_relative(store, REAL_HERMES_HOME)
        changed = False
        for job in enabled:
            jid = job.get("id", "?")
            name = job.get("name", "(unnamed)")
            job["enabled"] = False
            job["state"] = "paused"
            job["paused_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "+00:00"
            job["paused_reason"] = (
                f"{GUARD_MARKER} {store_label} {reason} "
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