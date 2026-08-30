#!/home/frank/.hermes/hermes-agent/venv/bin/python
"""Cron skip / silent-death monitor for every Hermes profile (chip task_017d7905).

Scans profiles/*/cron/jobs.json (plus the root cron/jobs.json). For each
ENABLED job it alerts when either:

  1. OVERDUE  — next_run_at is in the past by more than one schedule period
     (interval: `minutes`; cron: croniter-derived period; once: 1h grace).
     This is the true "scheduler skipped a fire" detector: a healthy ticker
     fires the job or fast-forwards next_run_at within one tick of the due
     time, so an overdue-by-a-full-period next_run_at means the store's
     ticker is dead, wedged, or the job is being silently skipped.

  2. NO-RUN-EVER — a recurring (cron/interval) job whose age exceeds
     max(3 days, 2x its period) with NO durable evidence of any run:
     no last_run_at in jobs.json AND zero rows in cron/executions.db.
     NOTE: executions.db keeps a bounded history; before the per-job
     retention floor (hermes-agent commit 61812cb8f) busy stores evicted
     low-frequency jobs' rows within hours, so last_run_at is checked too.
     (The 2026-08-05 git-clobber incident wiped last_run_at from live
     stores; jobs hit by BOTH lose all evidence and will flag here until
     their next successful run — that is deliberate: the monitor errs red.)

Prints one ALERT line per finding and exits 1 if any; exits 0 clean.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES = Path("/home/frank/.hermes")
ONESHOT_GRACE = timedelta(hours=1)
MIN_AGE = timedelta(days=3)

try:
    from croniter import croniter
except Exception:  # pragma: no cover - venv always has it; degrade gracefully
    croniter = None


def now() -> datetime:
    return datetime.now().astimezone()


def parse_ts(v):
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now().tzinfo)
    return dt


def schedule_period(schedule: dict) -> timedelta | None:
    kind = schedule.get("kind")
    if kind == "interval":
        m = schedule.get("minutes")
        if isinstance(m, (int, float)) and m > 0:
            return timedelta(minutes=m)
        return None
    if kind == "cron" and croniter is not None:
        expr = schedule.get("expr")
        if not expr:
            return None
        try:
            it = croniter(expr, now())
            a = it.get_next(datetime)
            b = it.get_next(datetime)
            return b - a
        except Exception:
            return None
    return None


def execution_count(exec_db: Path, job_id: str) -> int | None:
    if not exec_db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{exec_db}?mode=ro", uri=True, timeout=5)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM executions WHERE job_id=?", (job_id,)
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def check_store(label: str, cron_dir: Path, alerts: list[str]) -> None:
    jobs_file = cron_dir / "jobs.json"
    if not jobs_file.exists():
        return
    try:
        data = json.loads(jobs_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        alerts.append(f"ALERT [{label}] jobs.json unreadable: {e}")
        return
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    exec_db = cron_dir / "executions.db"
    t = now()
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if not job.get("enabled", True) or job.get("state") == "paused":
            continue
        jid = job.get("id", "?")
        name = job.get("name", "?")
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        kind = schedule.get("kind")
        period = schedule_period(schedule)

        # 1. OVERDUE: next_run_at further in the past than one period.
        nra = parse_ts(job.get("next_run_at"))
        allowance = period if period is not None else ONESHOT_GRACE
        if nra is not None and (t - nra) > allowance:
            alerts.append(
                f"ALERT [{label}] job {jid} ({name}): next_run_at {nra.isoformat()} "
                f"is {t - nra} in the past (> one period {allowance}) — "
                f"scheduler skipped/dead for this store"
            )
            continue
        if nra is None and kind in {"cron", "interval"}:
            alerts.append(
                f"ALERT [{label}] job {jid} ({name}): enabled recurring job has "
                f"no parseable next_run_at — will never fire"
            )
            continue

        # 2. NO-RUN-EVER: old recurring job with no durable run evidence.
        if kind not in {"cron", "interval"}:
            continue
        created = parse_ts(job.get("created_at"))
        if created is None:
            continue
        min_age = MIN_AGE
        if period is not None and 2 * period > min_age:
            min_age = 2 * period
        if (t - created) <= min_age:
            continue
        if parse_ts(job.get("last_run_at")) is not None:
            continue
        n = execution_count(exec_db, jid)
        if n is None or n == 0:
            alerts.append(
                f"ALERT [{label}] job {jid} ({name}): created {created.isoformat()} "
                f"(>{min_age} ago), zero recorded execution attempts and no "
                f"last_run_at — job has never verifiably run"
            )


def main() -> int:
    alerts: list[str] = []
    stores = sorted(HERMES.glob("profiles/*/cron")) + [HERMES / "cron"]
    seen: set[str] = set()
    for cron_dir in stores:
        if not cron_dir.is_dir():
            continue
        real = str(cron_dir.resolve())
        if real in seen:
            continue
        seen.add(real)
        label = (
            cron_dir.parent.name if cron_dir.parent.name != ".hermes" else "default"
        )
        check_store(label, cron_dir, alerts)
    stamp = now().isoformat()
    if alerts:
        print(f"{stamp} hermes_cron_skip_monitor: {len(alerts)} alert(s)")
        for a in alerts:
            print(a)
        return 1
    print(f"{stamp} hermes_cron_skip_monitor: OK — no skipped/never-run cron jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
