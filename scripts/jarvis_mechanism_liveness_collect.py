#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Collect the Jarvis commander LIGHT mechanism-liveness matrix.

This script is intentionally read-only. The paired agent cron consumes the JSON
and creates idempotency-keyed repair cards for rows marked DEAD; the collector
only classifies live mechanism evidence.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/home/frank/.hermes")
PROFILES = ROOT / "profiles"
JARVIS_HOME = PROFILES / "jarvis"
BOARDS = ROOT / "kanban" / "boards"
STATE_DIR = ROOT / "cron" / "state"
OUTPUT_ROOT = JARVIS_HOME / "cron" / "output"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_minutes(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 60.0)


def load_jobs(profile: str) -> list[dict[str, Any]]:
    path = PROFILES / profile / "cron" / "jobs.json"
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []
    return [j for j in jobs if isinstance(j, dict)]


def load_mark_job_run_drops(profile: str) -> dict[str, Any]:
    """Read the scheduler's durable terminal-write diagnostic for ``profile``.

    The sidecar is absent on healthy stores and is therefore not an error.  A
    malformed or unreadable sidecar is reported explicitly instead of being
    treated as zero, so the liveness report cannot hide diagnostic corruption.
    """
    path = PROFILES / profile / "cron" / "mark_job_run_drops.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"count": 0, "state": "absent", "path": str(path)}
    except (OSError, UnicodeError) as exc:
        return {"count": None, "state": "read_error", "error": str(exc), "path": str(path)}
    except json.JSONDecodeError as exc:
        return {"count": None, "state": "malformed", "error": str(exc), "path": str(path)}
    if not isinstance(data, dict):
        return {"count": None, "state": "malformed", "error": "expected JSON object", "path": str(path)}
    try:
        count = int(data.get("count", 0))
    except (TypeError, ValueError):
        return {"count": None, "state": "malformed", "error": "count is not an integer", "path": str(path)}
    if count < 0:
        return {"count": None, "state": "malformed", "error": "count is negative", "path": str(path)}
    return {
        "count": count,
        "state": "recorded" if count else "clean",
        "last_at": data.get("last_at"),
        "last_job_id": data.get("last_job_id"),
        "path": str(path),
    }


def all_profile_jobs() -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(PROFILES.glob("*/cron/jobs.json")):
        profile = path.parents[1].name
        for job in load_jobs(profile):
            out.append((profile, job))
    return out


def find_job(profile: str, name: str | None = None, script: str | None = None) -> tuple[str, dict[str, Any]] | None:
    candidates = [(profile, j) for j in load_jobs(profile)] if profile else all_profile_jobs()
    for prof, job in candidates:
        if name and job.get("name") == name:
            return prof, job
        if script and job.get("script") == script:
            return prof, job
    return None


def latest_output(job_id: str) -> dict[str, Any] | None:
    directory = OUTPUT_ROOT / job_id
    if not directory.exists():
        return None
    files = [p for p in directory.iterdir() if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    text = ""
    try:
        raw = newest.read_text(errors="replace")
        text = raw[-1200:]
    except Exception as exc:
        text = f"<read-error {type(exc).__name__}: {exc}>"
    return {"path": str(newest), "mtime": datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc).isoformat(), "tail": text}


def recent_comment_by_author(author: str, since_minutes: int = 1440) -> dict[str, Any] | None:
    cutoff = int(datetime.now(timezone.utc).timestamp()) - since_minutes * 60
    best: tuple[int, str, sqlite3.Row] | None = None
    for db in sorted(BOARDS.glob("*/kanban.db")):
        board = db.parent.name
        con: sqlite3.Connection | None = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, task_id, body, created_at FROM task_comments WHERE author=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                (author, cutoff),
            ).fetchall()
        except Exception:
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
        for row in rows:
            created = int(row["created_at"] or 0)
            if best is None or created > best[0]:
                best = (created, board, row)
    if not best:
        return None
    created, board, row = best
    return {
        "board": board,
        "task_id": row["task_id"],
        "comment_id": row["id"],
        "created_at_epoch": created,
        "body_excerpt": " ".join((row["body"] or "").split())[:500],
    }


# One-strike grace (t_631685fb): a single last_run age breach during a transient
# gateway ticker stall must not flip a mechanism to DEAD. Many of these jobs run
# every 5-15m; a one-cycle stall (observed ~20-30m gaps on 2026-07-25) is a
# ticker hiccup, not a mechanism rotation. The job is still OK if it is alive
# (last_status ok), within one scheduled period of its next run, and the breach
# is below this grace. Real rotations (paused, error, or persistently stale past
# GRACE + 1 period) still surface as DEAD.
LIVENESS_GRACE_MIN = 30

# Retryable-throttle grace (t_ed23d1d1): a single shared-provider-budget HTTP 429
# on an otherwise on-schedule job is a transient throttle, not a dead mechanism.
# The pattern is deliberately NARROW — auth failures, 5xx, timeouts and crashes
# are real deaths and must still return DEAD on the first sample. A bare mention
# of "rate limit" (e.g. a crash inside a rate_limit helper) is NOT enough: it must
# carry explicit throttle context (exceeded/reached/too many requests/retry).
RETRYABLE_THROTTLE_RE = re.compile(
    r"\b429\b"
    r"|\btoo many requests\b"
    r"|\brate[ _-]?limit(?:s|ed|ing)?\b(?=[^\n]{0,60}?\b(?:exceed\w*|reached|hit|retry|slow down|try again)\b)"
    r"|\b(?:exceed\w*|reached|hit)\b[^\n]{0,40}?\brate[ _-]?limit",
    re.IGNORECASE,
)

# Overdue tolerance for the throttle grace: next_run_at may sit slightly in the
# past due to clock skew or a run currently in flight. Anything more overdue than
# this is a wedged scheduler, NOT a job "armed to retry soon" -> DEAD.
OVERDUE_TOLERANCE_SEC = 300


def is_retryable_throttle(last_error: Any) -> bool:
    if not last_error:
        return False
    return bool(RETRYABLE_THROTTLE_RE.search(str(last_error)))


def consecutive_failed_runs(profile: str, job_id: str | None, limit: int = 10) -> int:
    """Count trailing consecutive failed executions for a job (read-only)."""
    if not job_id:
        return 0
    db = PROFILES / profile / "cron" / "executions.db"
    if not db.exists():
        return 0
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        rows = con.execute(
            "SELECT status FROM executions WHERE job_id=? AND status IN ('completed','failed') "
            "ORDER BY claimed_at DESC, id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
    except Exception:
        return 0
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
    streak = 0
    for (status,) in rows:
        if status == "failed":
            streak += 1
        else:
            break
    return streak


def classify_job(
    profile: str,
    job: dict[str, Any],
    now: datetime,
    max_age_minutes: int | None,
    allow_not_due: bool = True,
    consecutive_failures: int | None = None,
) -> tuple[str, str, float | None]:
    enabled = bool(job.get("enabled", True)) and job.get("state") != "paused"
    if not enabled:
        return "DEAD", "job paused/disabled", None
    last_run = parse_dt(job.get("last_run_at"))
    next_run = parse_dt(job.get("next_run_at"))
    created = parse_dt(job.get("created_at"))
    age = age_minutes(last_run, now)
    last_status = job.get("last_status")
    if last_status not in (None, "ok"):
        # One-strike retryable-throttle grace: a single 429 on a job that is
        # still armed to fire again within LIVENESS_GRACE_MIN is a transient
        # provider throttle -> WARN (non-gating). Two consecutive failed runs,
        # any non-throttle error, or a job not armed to run again soon is DEAD.
        streak = consecutive_failures
        if streak is None:
            streak = consecutive_failed_runs(profile, str(job.get("id") or ""))
        armed_soon = False
        if next_run is not None:
            delta = (next_run - now).total_seconds()
            # Two-sided window: must be due soon AND not significantly overdue.
            # An overdue next_run_at means the scheduler is wedged, which is a
            # real death, not a throttle to absorb (os-reviewer, t_a9f1d18a).
            armed_soon = -OVERDUE_TOLERANCE_SEC <= delta <= LIVENESS_GRACE_MIN * 60
        if (
            last_status == "error"
            and is_retryable_throttle(job.get("last_error"))
            and armed_soon
            and streak <= 1
        ):
            return "WARN", (
                f"last_status=error but retryable provider throttle (HTTP 429/rate limit) "
                f"with {streak} consecutive failed run(s); job armed to retry at "
                f"{next_run.isoformat()} within {LIVENESS_GRACE_MIN}m grace"
            ), age
        return "DEAD", f"last_status={last_status}", None
    if last_run is None:
        if allow_not_due and next_run and next_run > now:
            cadence = job.get("schedule", {}).get("kind")
            # First-run cron jobs can be healthy for days when registered before
            # their weekly/daily boundary. Do not mark an armed, not-yet-due
            # detector DEAD just because it was created before local midnight.
            if cadence == "cron" or created is None or created > now.replace(hour=0, minute=0, second=0, microsecond=0):
                return "OK", f"registered and not due yet; next scheduled {next_run.isoformat()}", None
        return "DEAD", "never run", None
    if max_age_minutes is not None and age is not None and age > max_age_minutes:
        if allow_not_due and next_run and next_run > now and job.get("schedule", {}).get("kind") == "cron":
            # Daily/weekly cron jobs can be healthy with age > max_age if they are not due.
            return "OK", f"last run age {age:.1f}m; next scheduled {next_run.isoformat()}", age
        # One-strike grace: absorb a single transient breach that is within
        # LIVENESS_GRACE_MIN of the window AND the job is still armed to run
        # again soon (next_run within grace of now). Persistently stale past
        # this grace is a real rotation and still returns DEAD.
        if (age - max_age_minutes) <= LIVENESS_GRACE_MIN and next_run is not None and (next_run - now).total_seconds() <= LIVENESS_GRACE_MIN * 60:
            return "OK", (
                f"last run age {age:.1f}m > {max_age_minutes}m but within "
                f"{LIVENESS_GRACE_MIN}m one-strike grace (transient ticker stall); "
                f"next scheduled {next_run.isoformat()}"
            ), age
        return "DEAD", f"stale last_run age {age:.1f}m > {max_age_minutes}m", age
    return "OK", "enabled, last_status ok, last_run fresh/enough", age


@dataclass(frozen=True)
class Expected:
    key: str
    label: str
    profile: str
    name: str | None = None
    script: str | None = None
    max_age_minutes: int | None = None
    required: bool = True


EXPECTED = [
    Expected("verdict-router", "verdict-router last_run + shadow/apply state", "jarvis", name="deterministic-verdict-router", max_age_minutes=30),
    Expected("wake-scanner", "wake scanner last_run + last wake action", "jarvis", name="kanban-scheduled-wake-scanner", max_age_minutes=30),
    Expected("pm-triage-jarvis-os", "PM triage cron: jarvis-os", "jarvis", name="elon-governance-loop", max_age_minutes=90),
    Expected("pm-triage-sycode-trading", "PM triage visibility bridge: sycode-trading", "jarvis", name="board-pm-triage-sycode-trading", max_age_minutes=90),
    Expected("pm-triage-sycode-ai", "PM triage visibility bridge: sycode-ai", "jarvis", name="board-pm-triage-sycode-ai", max_age_minutes=90),
    Expected("pm-triage-yorkstone-supplies", "PM triage visibility bridge: yorkstone-supplies", "jarvis", name="board-pm-triage-yorkstone-supplies", max_age_minutes=90),
    Expected("pm-triage-upero", "PM triage cron: upero", "jarvis", name="upero-pm-governance", max_age_minutes=90),
    Expected("registered-implies-ticking", "detector: registered-implies-ticking cron-health canary", "jarvis", name="cron-health-canary", max_age_minutes=90),
    Expected("black-hole-weekly", "detector: no-black-holes weekly", "jarvis", script="no_black_holes_detector.py", max_age_minutes=8 * 24 * 60),
    Expected("fork-drift", "detector: profile script fork drift", "jarvis", name="profile-script-drift-watch", max_age_minutes=36 * 60),
    Expected("quarantine-invariant", "detector: Sycode strategy quarantine invariant", "jarvis", name="sycode-strategy-quarantine-invariant-critical-alerts", max_age_minutes=36 * 60),
    Expected("leak-guard", "detector: Sycode canonical leak guard v2", "jarvis", name="sycode-canonical-leak-guard-v2-weekly", max_age_minutes=8 * 24 * 60),
    Expected("auto-review-router", "auto-review-router", "jarvis", name="review-required-auto-router", max_age_minutes=30),
    Expected("breaker", "breaker: codex exhaustion circuit breaker", "jarvis", name="codex-exhaustion-circuit-breaker", max_age_minutes=20),
    Expected("oob-canary", "OOB canary / alertmanager spool drain", "jarvis", name="sycode-alertmanager-oob-spool-drain", max_age_minutes=10),
    Expected("escalation-notifier-critical", "escalation notifier tier: blocked-task critical notifier", "jarvis", name="blocked-task-notifier", max_age_minutes=45),
    Expected("escalation-notifier-service-gate", "escalation notifier tier: service-gate escalation", "jarvis", name="dgx-service-gate-escalation", max_age_minutes=90),
]


def row_for_expected(exp: Expected, now: datetime) -> dict[str, Any]:
    match = find_job(exp.profile, name=exp.name, script=exp.script)
    if not match:
        return {
            "key": exp.key,
            "label": exp.label,
            "status": "DEAD" if exp.required else "WARN",
            "reason": "expected job not found in live cron stores",
            "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
            "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}",
            "expected": exp.__dict__,
        }
    profile, job = match
    status, reason, age = classify_job(profile, job, now, exp.max_age_minutes)
    drops = load_mark_job_run_drops(profile)
    diagnostic_error = job.get("last_error")
    if drops["state"] in {"malformed", "read_error"}:
        diagnostic_error = f"mark_job_run drop counter {drops['state']}: {drops.get('error', 'unknown error')}"
        status = "DEAD"
        reason = diagnostic_error
    elif drops.get("count", 0) > 0:
        diagnostic_error = (
            f"mark_job_run terminal metadata drops={drops['count']} "
            f"(last_at={drops.get('last_at')}, last_job_id={drops.get('last_job_id')})"
        )
        status = "DEAD"
        reason = diagnostic_error
    extra: dict[str, Any] = {}
    if exp.key == "verdict-router":
        extra["apply_state"] = "apply" if (STATE_DIR / "verdict-router.apply-enabled").exists() else "shadow"
        extra["sentinel"] = str(STATE_DIR / "verdict-router.apply-enabled")
    if exp.key == "wake-scanner":
        extra["last_wake_action"] = recent_comment_by_author("scheduled-wake-scanner", since_minutes=7 * 24 * 60)
    output = latest_output(str(job.get("id")))
    return {
        "key": exp.key,
        "label": exp.label,
        "status": status,
        "reason": reason,
        "profile": profile,
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "enabled": bool(job.get("enabled", True)),
        "state": job.get("state"),
        "schedule": job.get("schedule_display"),
        "next_run_at": job.get("next_run_at"),
        "last_run_at": job.get("last_run_at"),
        "last_age_minutes": None if age is None else round(age, 1),
        "last_status": job.get("last_status"),
        "last_error": diagnostic_error,
        "last_delivery_error": job.get("last_delivery_error"),
        "mark_job_run_drops": drops,
        "script": job.get("script"),
        "output_artifact": output,
        "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
        "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}",
        **extra,
    }


def extra_pm_visibility(now: datetime) -> list[dict[str, Any]]:
    """Non-gating visibility rows for boards without a known PM cron."""
    known = {"jarvis-os", "sycode-trading", "sycode-ai", "upero", "yorkstone-supplies"}
    rows: list[dict[str, Any]] = []
    for db in sorted(BOARDS.glob("*/kanban.db")):
        board = db.parent.name
        if board in known or board.startswith("_"):
            continue
        rows.append({
            "key": f"pm-triage-{board}",
            "label": f"PM triage cron visibility: {board}",
            "status": "WARN",
            "reason": "no live-verified board-specific PM triage cron registered in this matrix; visibility only, not a DEAD classification",
            "board": board,
        })
    return rows


def make_report(include_fixture: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    rows = [row_for_expected(exp, now) for exp in EXPECTED]
    rows.extend(extra_pm_visibility(now))
    if include_fixture:
        rows.append({
            "key": "fixture-paused-mechanism",
            "label": "FIXTURE: paused mechanism should route repair card",
            "status": "DEAD",
            "reason": "fixture-dead requested; simulates paused standing mechanism",
            "profile": "jarvis",
            "job_id": "fixture-paused",
            "job_name": "fixture-paused-mechanism",
            "enabled": False,
            "state": "paused",
            "repair_idempotency_key": "mechanism-liveness:fixture-paused-mechanism",
            "suggested_repair_title": "REPAIR mechanism liveness fixture: paused mechanism",
            "fixture": True,
        })
    dead = [r for r in rows if r.get("status") == "DEAD"]
    warn = [r for r in rows if r.get("status") == "WARN"]
    ok = [r for r in rows if r.get("status") == "OK"]
    return {
        "kind": "jarvis-commander-mechanism-liveness-light-matrix",
        "generated_at": now.isoformat(),
        "task_id": "t_5311fb77",
        "summary": {
            "overall": "GREEN" if not dead else "RED",
            "ok": len(ok),
            "dead": len(dead),
            "warn_visibility": len(warn),
            "dead_keys": [r.get("key") for r in dead],
        },
        "instructions_for_agent": [
            "For every row with status DEAD, create exactly one idempotency-keyed jarvis-os kanban repair card assigned to devops.",
            "Use the row repair_idempotency_key as the kanban idempotency key.",
            "Card body must name this cron, the row JSON, acceptance criteria to repair/verify the mechanism, and require the gap-plugging skill.",
            "Do not hand-fix mechanisms from the liveness cron.",
            "Deliver one line to discord:#fleet-reports: GREEN if dead=0, otherwise RED plus dead key list and created/existing repair card IDs.",
        ],
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dead", action="store_true", help="include a harmless simulated DEAD row for acceptance testing")
    args = parser.parse_args()
    report = make_report(include_fixture=args.fixture_dead)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
