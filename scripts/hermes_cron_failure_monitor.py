#!/usr/bin/env python3
# invoker: HOST crontab (minute 37 hourly) — chip task_8a90075d
#   manual: python3 ~/.hermes/scripts/hermes_cron_failure_monitor.py
#
# CONSECUTIVE-FAILURE MONITOR for hermes cron jobs (all profiles + root store).
# ---------------------------------------------------------------------------
# WHY: 2026-08-05 fleet-wide silent alert outage — jarvis alert-delivery cron
# jobs (spool drain abc411626232, firing bridge b0e293bc701c, kill-switch
# keepalive e909e7639aaf, r-multiple mutex 64b9e461b28f) failed every cycle
# with "Script not found" after live scripts were deleted from the cron root
# by a branch switch whose post-checkout restore hook was a no-op. Nothing
# watched the failure streaks, so the alert pipeline itself died silently
# ("silent-failure: plug AND monitor" — this is the monitor half).
#
# WHAT: reads the same stores `hermes cron runs` reads —
#   <cron_dir>/executions.db (table executions, status in claimed/running/
#   completed/failed/unknown) joined with <cron_dir>/jobs.json (enabled flag)
#   for cron_dir in ~/.hermes/cron and ~/.hermes/profiles/*/cron.
# For every ENABLED job whose last N (default 5) executions are ALL 'failed',
# prints one line and exits 1. Healthy fleet => quiet line, exit 0.
# Runs on HOST cron because hermes cron-kind scheduling itself is what can die.
#
# Read-only: opens sqlite in ro mode, never writes anything anywhere.
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
STREAK = int(os.environ.get("CRON_FAIL_STREAK", "5"))


def load_enabled_jobs(cron_dir: str) -> dict[str, str]:
    """job_id -> name for jobs whose enabled flag is true."""
    path = os.path.join(cron_dir, "jobs.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    out = {}
    for job in jobs or []:
        if isinstance(job, dict) and job.get("enabled") and job.get("id"):
            out[job["id"]] = job.get("name") or "?"
    return out


def last_statuses(db_path: str, job_id: str, n: int):
    """Newest-first last n executions: [(status, claimed_at, error), ...]."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as db:
        return db.execute(
            "SELECT status, claimed_at, COALESCE(error,'') FROM executions "
            "WHERE job_id=? ORDER BY claimed_at DESC, id DESC LIMIT ?",
            (job_id, n),
        ).fetchall()


def main() -> int:
    cron_dirs = [os.path.join(HERMES_HOME, "cron")] + sorted(
        glob.glob(os.path.join(HERMES_HOME, "profiles", "*", "cron"))
    )
    seen: set[str] = set()
    bad: list[str] = []
    scanned = 0
    for cron_dir in cron_dirs:
        real = os.path.realpath(cron_dir)
        if real in seen:
            continue
        seen.add(real)
        db_path = os.path.join(cron_dir, "executions.db")
        if not os.path.isfile(db_path):
            continue
        profile = os.path.basename(os.path.dirname(cron_dir)) or "root"
        if profile == ".hermes":
            profile = "root"
        for job_id, name in load_enabled_jobs(cron_dir).items():
            scanned += 1
            try:
                rows = last_statuses(db_path, job_id, STREAK)
            except sqlite3.Error as exc:
                bad.append(f"STORE-ERROR profile={profile} job={job_id} ({name}): {exc}")
                continue
            if len(rows) < STREAK:
                continue
            if all(status == "failed" for status, _, _ in rows):
                newest_err = (rows[0][2] or "").strip().splitlines()
                err = newest_err[0][:160] if newest_err else "no error recorded"
                bad.append(
                    f"FAIL-STREAK profile={profile} job={job_id} ({name}): "
                    f"last {STREAK} runs ALL failed; newest={rows[0][1]} err={err}"
                )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    if bad:
        print(f"HERMES CRON FAILURE MONITOR {stamp} — {len(bad)} enabled job(s) "
              f"with {STREAK} consecutive failures (scanned {scanned}):")
        for line in bad:
            print(line)
        return 1
    print(f"HERMES CRON FAILURE MONITOR {stamp} — healthy ({scanned} enabled jobs scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
