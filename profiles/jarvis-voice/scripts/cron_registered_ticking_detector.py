#!/usr/bin/env python3
"""Detect enabled Hermes cron jobs that are registered but not ticking.

No-agent cron contract: print nothing on clean scans. Print one structured alert
when any profile-local cron store has enabled jobs but no fresh ticker heartbeat,
or when an enabled job's last_run_at is older than 2x its schedule cadence.

Created for t_564bdeab after a devops profile-local cron store kept enabled jobs
for ~30h while no gateway was ticking it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any

PROFILE_ROOT = Path(os.environ.get("HERMES_CRON_STORE_PROFILE_ROOT", "/home/frank/.hermes/profiles"))
FRESHNESS_SECONDS = int(os.environ.get("HERMES_CRON_STORE_TICKER_FRESHNESS_SECONDS", "900"))
MAX_FINDINGS = int(os.environ.get("HERMES_CRON_STORE_MAX_FINDINGS", "40"))
STALE_MULTIPLIER = float(os.environ.get("HERMES_CRON_STORE_STALE_MULTIPLIER", "2"))
# Short-interval jobs can slip one or two gateway ticks during load; keep the
# detector focused on real registered-but-not-ticking failures, not one-minute jitter.
GRACE_SECONDS = int(os.environ.get("HERMES_CRON_STORE_GRACE_SECONDS", "300"))
STATE_PATH = Path(os.environ.get(
    "HERMES_CRON_REGISTERED_TICKING_STATE",
    "/home/frank/.hermes/var/cron_registered_ticking_detector_seen.json",
))
NOW = time.time()


def _finding_key(finding: dict[str, Any]) -> str:
    """Return a stable identity; volatile timestamps/ages are deliberately excluded."""
    return "|".join(str(finding.get(field, "")) for field in (
        "profile", "store", "kind", "job", "error",
    ))


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Emit only newly observed findings, while forgetting resolved findings."""
    current = {_finding_key(finding) for finding in findings}
    try:
        previous = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else []
        if not isinstance(previous, list):
            previous = []
        previous_keys = {str(key) for key in previous}
    except (OSError, ValueError, TypeError):
        previous_keys = set()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temp_path.write_text(json.dumps(sorted(current), separators=(",", ":")))
    temp_path.replace(STATE_PATH)
    return [finding for finding in findings if _finding_key(finding) not in previous_keys]


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(value: Any) -> float | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _cadence_seconds(schedule: dict[str, Any] | None, display: str | None = None) -> int | None:
    if not isinstance(schedule, dict):
        schedule = {}
    kind = schedule.get("kind")
    if kind == "interval":
        minutes = schedule.get("minutes")
        seconds = schedule.get("seconds")
        try:
            if seconds is not None:
                return max(1, int(seconds))
            if minutes is not None:
                return max(1, int(minutes) * 60)
        except (TypeError, ValueError):
            return None
    if kind == "cron":
        expr = str(schedule.get("expr") or display or "").strip()
        return _cron_cadence_seconds(expr)
    return _display_cadence_seconds(display or schedule.get("display"))


def _display_cadence_seconds(display: Any) -> int | None:
    if not display:
        return None
    text = str(display).strip().lower()
    m = re.fullmatch(r"(?:every\s+)?(\d+)\s*([smhd])", text)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _cron_cadence_seconds(expr: str) -> int | None:
    """Small conservative cadence parser for common 5-field Hermes cron specs."""
    parts = expr.split()
    if len(parts) != 5:
        return None
    minute, hour, _dom, _month, dow = parts
    if minute.startswith("*/"):
        try:
            return max(1, int(minute[2:]) * 60)
        except ValueError:
            return None
    if hour.startswith("*/"):
        try:
            return max(1, int(hour[2:]) * 3600)
        except ValueError:
            return None
    # Fixed minute + wildcard hour = hourly.
    if minute.isdigit() and hour == "*":
        return 3600
    # Day-of-week constrained jobs are at most weekly for stale detection.
    if dow not in {"*", "?"}:
        return 7 * 86400
    # Otherwise assume daily; this covers fixed minute/hour daily jobs.
    return 86400


def _job_ref(profile: str, job: dict[str, Any]) -> str:
    return f"{profile}:{job.get('id') or '?'}:{job.get('name') or '?'}"


def _load_jobs(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def _scan_store(profile_dir: Path) -> list[dict[str, Any]]:
    profile = profile_dir.name
    jobs_path = profile_dir / "cron" / "jobs.json"
    if not jobs_path.exists():
        return []
    try:
        jobs = _load_jobs(jobs_path)
    except Exception as exc:  # noqa: BLE001 - watchdog reports bad stores
        return [{"profile": profile, "store": str(jobs_path), "kind": "unreadable_store", "error": f"{type(exc).__name__}: {exc}"}]

    enabled = [job for job in jobs if job.get("enabled", True)]
    if not enabled:
        return []

    findings: list[dict[str, Any]] = []
    heartbeat = profile_dir / "cron" / "ticker_heartbeat"
    heartbeat_age = None
    if heartbeat.exists():
        heartbeat_age = int(NOW - heartbeat.stat().st_mtime)
    if heartbeat_age is None or heartbeat_age > FRESHNESS_SECONDS:
        findings.append(
            {
                "profile": profile,
                "store": str(jobs_path),
                "kind": "missing_or_stale_ticker_heartbeat",
                "enabled_jobs": len(enabled),
                "heartbeat_age_seconds": heartbeat_age,
                "freshness_seconds": FRESHNESS_SECONDS,
            }
        )

    for job in enabled:
        cadence = _cadence_seconds(job.get("schedule"), job.get("schedule_display"))
        if cadence is None:
            continue
        threshold = max(int(cadence * STALE_MULTIPLIER), cadence + GRACE_SECONDS)
        last_run = _parse_iso(job.get("last_run_at"))
        compare_from = last_run if last_run is not None else _parse_iso(job.get("created_at"))
        if compare_from is None:
            continue
        age = int(NOW - compare_from)
        # If created in the future due to clock skew, do not fire this stale rule; absurd
        # timestamps are covered by the kanban integrity watchdog for board rows.
        if age > threshold:
            findings.append(
                {
                    "profile": profile,
                    "store": str(jobs_path),
                    "kind": "enabled_job_last_run_stale",
                    "job": _job_ref(profile, job),
                    "last_run_at": job.get("last_run_at"),
                    "created_at": job.get("created_at"),
                    "age_seconds": age,
                    "threshold_seconds": threshold,
                    "schedule": job.get("schedule") or job.get("schedule_display"),
                }
            )
    return findings


def main() -> int:
    findings: list[dict[str, Any]] = []
    for profile_dir in sorted(p for p in PROFILE_ROOT.iterdir() if p.is_dir()):
        findings.extend(_scan_store(profile_dir))
        if len(findings) >= MAX_FINDINGS:
            findings = findings[:MAX_FINDINGS]
            break

    if findings:
        findings = _dedupe_findings(findings)
    if findings:
        payload = {
            "ts": _utc_now(),
            "error": "enabled cron jobs registered but not ticking or stale",
            "profile_root": str(PROFILE_ROOT),
            "ticker_freshness_seconds": FRESHNESS_SECONDS,
            "stale_multiplier": STALE_MULTIPLIER,
            "findings": findings,
        }
        print("CRON_REGISTERED_NOT_TICKING_ALERT " + json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
