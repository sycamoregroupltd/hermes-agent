#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Cron health meta-canary — no-agent cron.

Reads every profile-local ``~/.hermes/profiles/*/cron/jobs.json`` directly,
not the caller-scoped ``hermes cron list`` view. Silent when healthy. Emits a
single actionable alert when an enabled job has ``last_status=error`` (or a
delivery error) or is overdue by more than 2x its interval/cron cadence.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REAL_HERMES_HOME = Path(os.environ.get("HERMES_REAL_HOME", "/home/frank/.hermes")).expanduser()
if not (REAL_HERMES_HOME / "profiles").exists() and (REAL_HERMES_HOME / ".hermes" / "profiles").exists():
    REAL_HERMES_HOME = REAL_HERMES_HOME / ".hermes"
PROFILES_DIR = REAL_HERMES_HOME / "profiles"
MAX_ALERTS = int(os.environ.get("CRON_HEALTH_MAX_ALERTS", "25"))


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def interval_seconds(job: dict[str, Any]) -> int | None:
    schedule = job.get("schedule") or {}
    if schedule.get("kind") == "interval":
        minutes = schedule.get("minutes")
        try:
            return int(float(str(minutes)) * 60)
        except Exception:
            return None
    if schedule.get("kind") == "cron":
        expr = str(schedule.get("expr") or job.get("schedule_display") or "").strip()
        parts = expr.split()
        if len(parts) != 5:
            return None
        minute, hour = parts[0], parts[1]
        if minute.startswith("*/"):
            try:
                return int(minute[2:]) * 60
            except Exception:
                return None
        if hour.startswith("*/"):
            try:
                return int(hour[2:]) * 3600
            except Exception:
                return None
        if hour == "*":
            return 3600
        return 24 * 3600
    return None


def iter_profile_jobs() -> list[tuple[str, Path, dict[str, Any]]]:
    rows: list[tuple[str, Path, dict[str, Any]]] = []
    if not PROFILES_DIR.exists():
        rows.append(("<scan>", PROFILES_DIR, {"name": "<profiles>", "enabled": True, "last_status": "error", "last_error": f"profile cron root missing: {PROFILES_DIR}"}))
        return rows
    job_paths = sorted(PROFILES_DIR.glob("*/cron/jobs.json"))
    if not job_paths:
        rows.append(("<scan>", PROFILES_DIR, {"name": "<profiles>", "enabled": True, "last_status": "error", "last_error": f"zero profile cron stores matched under {PROFILES_DIR}"}))
        return rows
    for path in job_paths:
        profile = path.parts[-3]
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            rows.append((profile, path, {"name": "<jobs.json>", "enabled": True, "last_status": "error", "last_error": f"unreadable jobs.json: {exc}"}))
            continue
        for job in data.get("jobs", []):
            if isinstance(job, dict):
                rows.append((profile, path, job))
    return rows


def check_script_resolution(profile: str, job: dict[str, Any]) -> str | None:
    """Pre-flight for the silent-dark path-mismatch class.

    The cron scheduler resolves a job's ``script`` against that profile's own
    ``$HERMES_HOME/scripts`` dir at tick time (scheduler._run_job_script).
    A script that exists ONLY in the GLOBAL ``~/.hermes/scripts/`` (not the
    profile-local ``~/.hermes/profiles/<p>/scripts/``) errors every tick with
    'Script not found' while the canary can stay silent — the exact defect this
    task closes. This check flags it BEFORE the next tick instead of after
    hours of silent failure.

    Returns an alert line if the job would FAIL resolution, else None.
    """
    script = job.get("script")
    if not script:
        return None
    raw = Path(script).expanduser()
    if raw.is_absolute():
        return None  # resolver itself blocks/validates absolute paths; other class
    local_dir = (REAL_HERMES_HOME / "profiles" / profile / "scripts").resolve()
    local_path = (local_dir / raw).resolve()
    global_dir = (REAL_HERMES_HOME / "scripts").resolve()
    global_path = (global_dir / raw).resolve()
    # Replicate the resolver's within-dir guard; if it would escape, the
    # resolver blocks it on a different code path — not our class.
    try:
        local_path.relative_to(local_dir)
    except ValueError:
        return None
    if local_path.exists() and local_path.is_file():
        return None
    name = job.get("name") or job.get("id") or "<unnamed>"
    prefix = f"{profile}/{name}"
    if global_path.exists() and global_path.is_file():
        return (
            f"PATH-MISMATCH {prefix}: script '{script}' resolves ONLY to the "
            f"GLOBAL {global_dir} (not profile-local {local_dir}) — will error "
            f"'Script not found' every tick until copied into the profile dir"
        )
    return (
        f"PATH-MISMATCH {prefix}: script '{script}' missing from profile-local "
        f"{local_dir} (and global) — job will fail on next tick"
    )


def main() -> None:
    now = datetime.now(timezone.utc)
    bad: list[str] = []
    scanned_profiles: set[str] = set()
    scanned_jobs = 0

    for profile, path, job in iter_profile_jobs():
        scanned_profiles.add(profile)
        if not job.get("enabled", True):
            continue
        scanned_jobs += 1
        name = job.get("name") or job.get("id") or "<unnamed>"
        prefix = f"{profile}/{name}"
        status = str(job.get("last_status") or "").lower()
        if status in {"error", "failed"}:
            reason = str(job.get("last_error") or "last_status=error")[:180]
            bad.append(f"ERROR {prefix}: {reason}")
        if job.get("last_delivery_error"):
            bad.append(f"DELIVERY {prefix}: {str(job.get('last_delivery_error'))[:180]}")

        # Pre-flight: catch the silent-dark path-mismatch class BEFORE the tick.
        path_alert = check_script_resolution(profile, job)
        if path_alert:
            bad.append(path_alert)

        cadence = interval_seconds(job)
        if cadence:
            last = parse_dt(job.get("last_run_at"))
            next_run = parse_dt(job.get("next_run_at"))
            if last and (now - last).total_seconds() > 2 * cadence + 300:
                age_h = (now - last).total_seconds() / 3600
                bad.append(f"OVERDUE {prefix}: last_run_age={age_h:.1f}h > 2x cadence ({cadence // 60}m)")
            elif next_run and (now - next_run).total_seconds() > cadence + 300:
                late_h = (now - next_run).total_seconds() / 3600
                bad.append(f"OVERDUE {prefix}: next_run_at is {late_h:.1f}h in the past")

    if bad:
        shown = bad[:MAX_ALERTS]
        lines = [f"🔴 CRON HEALTH: {len(bad)} issue(s) across {len(scanned_profiles)} profile cron store(s), {scanned_jobs} enabled job(s) scanned"]
        lines.extend(f"  • {item}" for item in shown)
        if len(bad) > len(shown):
            lines.append(f"  • … {len(bad) - len(shown)} more")
        lines.append(f"Source: direct {PROFILES_DIR}/*/cron/jobs.json scan; silent when healthy.")
        print("\n".join(lines))


if __name__ == "__main__":
    main()

