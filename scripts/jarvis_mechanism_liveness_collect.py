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

    ``cron/jobs.py:mark_job_run()`` persists ``cron/mark_job_run_drops.json``
    (t_95fbd07c) whenever a completed execution's terminal metadata write
    could not find its job record (finish_execution succeeded but
    last_run_at was never stamped). The sidecar is absent on healthy stores
    and that is not an error. A malformed or unreadable sidecar is reported
    explicitly instead of being treated as zero, so this collector cannot
    silently hide diagnostic corruption.
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
    seen: set[str] = set()
    for path in sorted(PROFILES.glob("*/cron/jobs.json")):
        real = str(path.resolve())
        if real in seen:
            continue
        seen.add(real)
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


KANBAN_MANIFEST_PATH = ROOT / "kanban" / "boards-manifest.json"

PM_TRIAGE_KEY_PREFIX = "pm-triage-"


def load_boards_manifest() -> dict[str, Any] | None:
    """Read-only load of the fleet boards manifest (see scripts/fleet_boards.py).

    Returns None on any missing/malformed manifest so callers can fail
    visibly (keep the real classify_job() verdict) instead of silently
    treating an unreadable manifest as "board is dormant".
    """
    try:
        data = json.loads(KANBAN_MANIFEST_PATH.read_text())
    except Exception:
        return None
    boards = data.get("boards") if isinstance(data, dict) else None
    if not isinstance(boards, dict):
        return None
    return boards


def pm_triage_board_name(key: str) -> str | None:
    if not key.startswith(PM_TRIAGE_KEY_PREFIX):
        return None
    board = key[len(PM_TRIAGE_KEY_PREFIX):]
    return board or None


def apply_pm_triage_manifest_override(row: dict[str, Any], exp: "Expected") -> dict[str, Any]:
    """t_6f8c78be: defer pm-triage-<board> DEAD verdicts to the manifest's triage flag.

    classify_job() correctly treats a paused/disabled job as DEAD in general
    -- that IS the right call for a mechanism that is supposed to be
    ticking. But a `pm-triage-<board>` row whose board the fleet boards
    manifest (scripts/fleet_boards.py's source of truth) has explicitly
    marked not-triaged (triage: false, e.g. dormant/denied/aliased) is NOT
    supposed to be ticking: pausing (or never wiring) its cron job is the
    correct state, not a mechanism death. This only ever overrides an
    existing DEAD verdict, and only when the manifest is readable AND
    explicitly says triage=false for that exact board -- an unreadable
    manifest, an unknown board, or a board still expecting triage=true
    leaves the real classify_job()/row_for_expected() verdict untouched
    (fail-visible; no permanent hardcoded removal of the Expected entry).
    If the board is later reactivated (manifest triage flips back to true),
    this stops overriding on the very next collector run with no code
    change -- the mechanism goes back to expecting a live ticking job.
    """
    if row.get("status") != "DEAD":
        return row
    board = pm_triage_board_name(exp.key)
    if board is None:
        return row
    boards = load_boards_manifest()
    if boards is None:
        return row  # manifest unreadable: fail visible, keep real DEAD verdict
    cfg = boards.get(board)
    if not isinstance(cfg, dict) or cfg.get("triage") is not False:
        return row  # unknown board, or manifest still expects triage -> real DEAD stands
    overridden = dict(row)
    overridden["status"] = "OK"
    overridden["reason"] = (
        f"retired: board {board!r} manifest state={cfg.get('state', 'unknown')!r}, "
        f"triage=false (board not expected to run PM triage); "
        f"underlying classify_job verdict: {row.get('reason')}"
    )
    overridden["manifest_triage_override"] = {
        "board": board,
        "manifest_state": cfg.get("state"),
        "manifest_reason": cfg.get("reason"),
        "underlying_status": "DEAD",
        "underlying_reason": row.get("reason"),
    }
    return overridden


@dataclass(frozen=True)
class Expected:
    key: str
    label: str
    profile: str
    name: str | None = None
    script: str | None = None
    max_age_minutes: int | None = None
    required: bool = True
    # When a source row was paused by CONDENSE — or the paused source row was
    # later pruned — classify against this exact runner check rather than
    # treating the absorbed mechanism as a missing job.
    bundle_check: str | None = None


# Stable source-row -> runner-check aliases. Keep this explicit: absent or
# failed bundle checks must remain DEAD, and provider-owned PM rows are not
# absorbed merely because a bundle exists.
BUNDLE_ALIASES = {
    "registered-implies-ticking": ("guard-bundle-tick-15m", "cron-health-canary"),
    "black-hole-weekly": ("guard-bundle-tick-daily", "standing-no-black-holes-detector"),
    "leak-guard": ("guard-bundle-tick-daily", "sycode-canonical-leak-guard-v2-weekly"),
    "escalation-notifier-service-gate": ("guard-bundle-tick-15m", "dgx-service-gate-escalation"),
}


EXPECTED = [
    Expected("verdict-router", "verdict-router last_run + shadow/apply state", "jarvis", name="deterministic-verdict-router", max_age_minutes=30),
    Expected("wake-scanner", "wake scanner last_run + last wake action", "jarvis", name="kanban-scheduled-wake-scanner", max_age_minutes=30),
    Expected("pm-triage-jarvis-os", "PM triage visibility bridge: jarvis-os", "jarvis", name="board-pm-triage-jarvis-os", max_age_minutes=90),
    Expected("pm-triage-sycode-trading", "PM triage visibility bridge: sycode-trading", "jarvis", name="board-pm-triage-sycode-trading", max_age_minutes=90),
    Expected("pm-triage-sycode-ai", "PM triage visibility bridge: sycode-ai", "jarvis", name="board-pm-triage-sycode-ai", max_age_minutes=90),
    Expected("pm-triage-yorkstone-supplies", "PM triage visibility bridge: yorkstone-supplies", "jarvis", name="board-pm-triage-yorkstone-supplies", max_age_minutes=90),
    Expected("pm-triage-upero", "PM triage cron: upero", "jarvis", name="upero-pm-governance", max_age_minutes=90),
    Expected("registered-implies-ticking", "detector: registered-implies-ticking cron-health canary", "jarvis", name="cron-health-canary", max_age_minutes=90, bundle_check="cron-health-canary"),
    Expected("black-hole-weekly", "detector: no-black-holes weekly", "jarvis", script="no_black_holes_detector.py", max_age_minutes=8 * 24 * 60, bundle_check="standing-no-black-holes-detector"),
    Expected("fork-drift", "detector: profile script fork drift", "jarvis", name="profile-script-drift-watch", max_age_minutes=36 * 60),
    Expected("quarantine-invariant", "detector: Sycode strategy quarantine invariant", "jarvis", name="sycode-strategy-quarantine-invariant-critical-alerts", max_age_minutes=36 * 60),
    Expected("leak-guard", "detector: Sycode canonical leak guard v2", "jarvis", name="sycode-canonical-leak-guard-v2-weekly", max_age_minutes=8 * 24 * 60, bundle_check="sycode-canonical-leak-guard-v2-weekly"),
    Expected("auto-review-router", "auto-review-router", "jarvis", name="review-required-auto-router", max_age_minutes=30),
    Expected("breaker", "breaker: codex exhaustion circuit breaker", "jarvis", name="codex-exhaustion-circuit-breaker", max_age_minutes=20),
    Expected("oob-canary", "OOB canary / alertmanager spool drain", "jarvis", name="sycode-alertmanager-oob-spool-drain", max_age_minutes=10),
    Expected("escalation-notifier-critical", "escalation notifier tier: blocked-task critical notifier", "jarvis", name="blocked-task-notifier", max_age_minutes=45),
    Expected("escalation-notifier-service-gate", "escalation notifier tier: service-gate escalation", "jarvis", name="dgx-service-gate-escalation", max_age_minutes=90, bundle_check="dgx-service-gate-escalation"),
    # ADDITIVE (t_673f6cba): completion-gate latency tripwire consumer. Unlike
    # the rows above it has no paused CONDENSE source row — it is a brand-new
    # guard-bundle check. row_for_expected() classifies it by its OWN per-check
    # state (direct-additive branch); required=False keeps it WARN (visibility)
    # until the first 15m tick writes per-check state, so landing it cannot
    # fabricate a DEAD repair card.
    Expected("completion-gate-latency-watch", "completion-gate latency tripwire consumer (guard-bundle 15m)", "jarvis", name="guard-bundle-tick-15m", bundle_check="gate-kanban-complete-latency-watch", max_age_minutes=30, required=False),
]


def load_bundle_check(check_name: str) -> dict[str, Any] | None:
    """Read the runner manifest without starting a check or changing state."""
    runner = JARVIS_HOME / "scripts" / "cron_guard_bundle_runner.py"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("jarvis_guard_bundle_runner", runner)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.CHECKS.get(check_name)
    except Exception:
        return None


def guard_bundle_state_path() -> Path:
    """Path to the runner's per-check state file (mirrors cron_guard_bundle_runner.py)."""
    return JARVIS_HOME / "cron" / "state" / "guard_bundle_last_run.json"


def load_guard_bundle_state() -> dict[str, Any]:
    """Read-only load of the guard-bundle runner's per-check state file.

    Never mutates the file (the collector must stay read-only). Returns {}
    on any read/parse error so a missing/corrupt state file degrades to the
    fail-visible DEAD path in bundle_check_status rather than crashing.
    """
    try:
        return json.loads(guard_bundle_state_path().read_text())
    except Exception:
        return {}


def bundle_check_status(
    check_name: str,
    bundle_job: dict[str, Any],
    now: datetime,
    max_age_minutes: int | None,
    state: dict[str, Any] | None = None,
) -> tuple[str, str, float | None]:
    """Classify one guard-bundle-absorbed check by its OWN recorded outcome.

    t_a781c1f2: the previous implementation called classify_job() on the
    whole bundle cron job record, so ANY sibling check's failure that tick
    flipped every other (healthy) check in the same bundle to DEAD too
    (reproduced live: standing-no-black-holes-detector failing in
    guard-bundle-tick-daily false-DEADed the unrelated, genuinely clean
    leak-guard check). cron_guard_bundle_runner.py now persists each check's
    own last_status/last_error into guard_bundle_last_run.json
    (f"{name}:last_status" / f"{name}:last_error") alongside the pre-existing
    per-check last-run timestamp; classify from that, not the bundle
    aggregate.

    Bundle-level gates (the whole bundle disabled/paused, or never ticked at
    all) still apply first: a bundle that has never fired can't have run any
    of its checks, regardless of what stale per-check state might say.
    """
    enabled = bool(bundle_job.get("enabled", True)) and bundle_job.get("state") != "paused"
    if not enabled:
        return "DEAD", "guard bundle job paused/disabled", None
    if not bundle_job.get("last_run_at"):
        return "DEAD", "guard bundle job never run", None
    if state is None:
        state = load_guard_bundle_state()
    raw_ts = state.get(check_name)
    if not raw_ts:
        return "DEAD", f"check '{check_name}' has no per-check state entry (never run within the bundle)", None
    try:
        last_run = datetime.fromtimestamp(int(raw_ts), timezone.utc)
    except Exception:
        return "DEAD", f"check '{check_name}' per-check timestamp is unparseable: {raw_ts!r}", None
    age = age_minutes(last_run, now)
    per_status = state.get(f"{check_name}:last_status")
    if per_status is None:
        # Old-format state file written before t_a781c1f2 (timestamp only, no
        # pass/fail key). Degrade safely: fail-visible DEAD rather than a
        # KeyError or a silently-inherited wrong verdict. The next bundle
        # tick writes the new keys and this self-heals.
        return "DEAD", (
            f"check '{check_name}' last ran at {last_run.isoformat()} but its state "
            "predates per-check status tracking (old-format guard_bundle_last_run.json); "
            "will self-heal on the next bundle tick"
        ), age
    if per_status != "ok":
        err = state.get(f"{check_name}:last_error") or f"last_status={per_status}"
        return "DEAD", f"check '{check_name}' own last run failed: {err}", age
    if max_age_minutes is not None and age is not None and age > max_age_minutes:
        return "DEAD", f"check '{check_name}' own last-run age {age:.1f}m > {max_age_minutes}m", age
    return "OK", (
        f"check '{check_name}' own last-run status ok"
        + (f", age {age:.1f}m" if age is not None else "")
    ), age


def bundle_row_for_expected(exp: Expected) -> tuple[str, str, dict[str, Any]] | None:
    """Return the live bundle row and manifest check for a condensed source row."""
    alias = BUNDLE_ALIASES.get(exp.key)
    if not alias:
        return None
    bundle_name, check_name = alias
    match = find_job("jarvis", name=bundle_name)
    if not match:
        return None
    return bundle_name, check_name, match[1]


def classify_absorbed_bundle_row(
    exp: Expected,
    now: datetime,
    bundle_name: str,
    bundle_job: dict[str, Any],
    reason_prefix: str,
) -> dict[str, Any]:
    """Classify an absorbed mechanism by its own guard-bundle check.

    Shared by CONDENSE-paused source rows and by BUNDLE_ALIASES keys whose
    source row has already been pruned. Per-check state (t_a781c1f2) is the
    verdict; the bundle job's aggregate last_status stays visibility-only.
    """
    check_name = exp.bundle_check or ""
    check = load_bundle_check(check_name)
    if check is None:
        return {
            "key": exp.key,
            "label": exp.label,
            "status": "DEAD",
            "reason": f"{reason_prefix}: bundle check missing from runner manifest",
            "expected": exp.__dict__,
            "bundle_check": exp.bundle_check,
            "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
            "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}",
        }
    own_state = load_guard_bundle_state()
    status, reason, age = bundle_check_status(
        check_name, bundle_job, now, exp.max_age_minutes, state=own_state
    )
    check_path = (JARVIS_HOME / "scripts" / str(check.get("script", ""))).resolve()
    if not check_path.is_file():
        status, reason = "DEAD", f"bundle check script missing: {check_path}"
    own_status = own_state.get(f"{check_name}:last_status")
    own_error = own_state.get(f"{check_name}:last_error")
    own_ts = own_state.get(check_name)
    own_last_run_at = None
    if own_ts:
        try:
            own_last_run_at = datetime.fromtimestamp(int(own_ts), timezone.utc).isoformat()
        except Exception:
            own_last_run_at = None
    return {
        "key": exp.key,
        "label": exp.label,
        "status": status,
        "reason": f"{reason_prefix}: {reason}",
        "profile": "jarvis",
        "job_id": bundle_job.get("id"),
        "job_name": bundle_name,
        "enabled": bool(bundle_job.get("enabled", True)),
        "state": bundle_job.get("state"),
        "schedule": bundle_job.get("schedule_display"),
        "next_run_at": bundle_job.get("next_run_at"),
        "last_run_at": own_last_run_at or bundle_job.get("last_run_at"),
        "last_age_minutes": None if age is None else round(age, 1),
        "last_status": own_status if own_status is not None else bundle_job.get("last_status"),
        "last_error": own_error if own_status not in (None, "ok") else None,
        "bundle_last_run_at": bundle_job.get("last_run_at"),
        "bundle_last_status": bundle_job.get("last_status"),
        "bundle_last_error": bundle_job.get("last_error"),
        "last_delivery_error": bundle_job.get("last_delivery_error"),
        "script": check.get("script"),
        "bundle": bundle_name,
        "bundle_check": exp.bundle_check,
        "producer": f"{JARVIS_HOME}/scripts/cron_guard_bundle_runner.py:{exp.bundle_check}",
        "consumer": "local -> /home/frank/.hermes/scripts/report-to-board.py -> jarvis-os -> jarvis-os-pm",
        "output_artifact": latest_output(str(bundle_job.get("id"))),
        "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
        "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}",
    }


def row_for_expected(exp: Expected, now: datetime) -> dict[str, Any]:
    match = find_job(exp.profile, name=exp.name, script=exp.script)
    if match:
        profile, job = match
        if exp.bundle_check and job.get("name") == exp.name and str(exp.name).startswith("guard-bundle-tick-"):
            # ADDITIVE guard-bundle check with no paused CONDENSE source row:
            # classify the CHECK's OWN recorded state (t_a781c1f2 semantics),
            # not the whole bundle aggregate. required=False keeps a brand-new
            # check at WARN (visibility) until its first bundle tick writes
            # per-check state, so landing it cannot fabricate a DEAD repair card.
            bundle_name, bundle_job = job.get("name"), job
            check = load_bundle_check(exp.bundle_check)
            if check is None:
                return {"key": exp.key, "label": exp.label, "status": "DEAD",
                        "reason": f"additive bundle check missing from runner manifest: {exp.bundle_check}",
                        "expected": exp.__dict__, "bundle_check": exp.bundle_check,
                        "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
                        "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}"}
            own_state = load_guard_bundle_state()
            if not own_state.get(exp.bundle_check) and not exp.required:
                status, reason, age = "WARN", (
                    f"guard-bundle additive check {bundle_name}/{exp.bundle_check} has no "
                    "per-check state yet (first 15m tick will populate); visibility only, not DEAD"
                ), None
            else:
                status, reason, age = bundle_check_status(exp.bundle_check, bundle_job, now, exp.max_age_minutes, state=own_state)
            check_path = (JARVIS_HOME / "scripts" / str(check.get("script", ""))).resolve()
            if not check_path.is_file():
                status, reason = "DEAD", f"bundle check script missing: {check_path}"
            own_status = own_state.get(f"{exp.bundle_check}:last_status")
            own_error = own_state.get(f"{exp.bundle_check}:last_error")
            own_ts = own_state.get(exp.bundle_check)
            own_last_run_at = None
            if own_ts:
                try:
                    own_last_run_at = datetime.fromtimestamp(int(own_ts), timezone.utc).isoformat()
                except Exception:
                    own_last_run_at = None
            return {
                "key": exp.key, "label": exp.label, "status": status,
                "reason": f"guard-bundle additive check {bundle_name}/{exp.bundle_check}: {reason}",
                "profile": "jarvis", "job_id": bundle_job.get("id"), "job_name": bundle_name,
                "enabled": bool(bundle_job.get("enabled", True)), "state": bundle_job.get("state"),
                "schedule": bundle_job.get("schedule_display"), "next_run_at": bundle_job.get("next_run_at"),
                "last_run_at": own_last_run_at or bundle_job.get("last_run_at"),
                "last_age_minutes": None if age is None else round(age, 1),
                "last_status": own_status if own_status is not None else bundle_job.get("last_status"),
                "last_error": own_error if own_status not in (None, "ok") else None,
                "bundle_last_run_at": bundle_job.get("last_run_at"),
                "bundle_last_status": bundle_job.get("last_status"),
                "bundle_last_error": bundle_job.get("last_error"),
                "last_delivery_error": bundle_job.get("last_delivery_error"), "script": check.get("script"),
                "bundle": bundle_name, "bundle_check": exp.bundle_check,
                "producer": f"{JARVIS_HOME}/scripts/cron_guard_bundle_runner.py:{exp.bundle_check}",
                "consumer": "local -> /home/frank/.hermes/scripts/report-to-board.py -> jarvis-os -> jarvis-os-pm",
                "output_artifact": latest_output(str(bundle_job.get("id"))),
                "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
                "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}"}
        if exp.bundle_check and job.get("state") == "paused" and "CONDENSE 1/4" in str(job.get("paused_reason", "")):
            bundle_match = bundle_row_for_expected(exp)
            if not bundle_match:
                return {"key": exp.key, "label": exp.label, "status": "DEAD",
                        "reason": "condensed source row is paused but bundle check is missing",
                        "expected": exp.__dict__, "bundle_check": exp.bundle_check,
                        "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
                        "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}"}
            bundle_name, _check_name, bundle_job = bundle_match
            return classify_absorbed_bundle_row(
                exp, now, bundle_name, bundle_job,
                f"condensed into {bundle_name}/{exp.bundle_check}",
            )
    if not match:
        # Source row gone (pruned after CONDENSE). If this key is absorbed into
        # a live guard-bundle check, classify that producer instead of filing
        # a false-DEAD missing-job repair. Non-absorbed keys still DEAD.
        if exp.bundle_check and exp.key in BUNDLE_ALIASES:
            bundle_match = bundle_row_for_expected(exp)
            if bundle_match:
                bundle_name, _check_name, bundle_job = bundle_match
                return classify_absorbed_bundle_row(
                    exp, now, bundle_name, bundle_job,
                    f"condensed into {bundle_name}/{exp.bundle_check} (source job gone)",
                )
        return apply_pm_triage_manifest_override({
            "key": exp.key,
            "label": exp.label,
            "status": "DEAD" if exp.required else "WARN",
            "reason": "expected job not found in live cron stores",
            "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
            "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}",
            "expected": exp.__dict__,
        }, exp)
    profile, job = match
    status, reason, age = classify_job(profile, job, now, exp.max_age_minutes)
    extra: dict[str, Any] = {}
    if exp.key == "verdict-router":
        extra["apply_state"] = "apply" if (STATE_DIR / "verdict-router.apply-enabled").exists() else "shadow"
        extra["sentinel"] = str(STATE_DIR / "verdict-router.apply-enabled")
    if exp.key == "wake-scanner":
        extra["last_wake_action"] = recent_comment_by_author("scheduled-wake-scanner", since_minutes=7 * 24 * 60)
    output = latest_output(str(job.get("id")))
    last_run_at = job.get("last_run_at")
    last_error = job.get("last_error")
    # t_95fbd07c: mark_job_run() can drop a completed execution's terminal
    # write when its job record isn't found at write time (finish_execution
    # still records success). Surface that drop as a probe-visible DEAD row
    # instead of a silent stale last_run_at — this is the named consumer for
    # cron/jobs.py's mark_job_run_drops.json sidecar.
    drops = load_mark_job_run_drops(profile)
    if drops["state"] in {"malformed", "read_error"}:
        status = "DEAD"
        last_error = f"mark_job_run drop counter {drops['state']}: {drops.get('error', 'unknown error')}"
        reason = last_error
    elif drops.get("count", 0) > 0:
        status = "DEAD"
        last_error = (
            f"mark_job_run terminal metadata drops={drops['count']} "
            f"(last_at={drops.get('last_at')}, last_job_id={drops.get('last_job_id')})"
        )
        reason = last_error
    return apply_pm_triage_manifest_override({
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
        "last_run_at": last_run_at,
        "last_age_minutes": None if age is None else round(age, 1),
        "last_status": job.get("last_status"),
        "last_error": last_error,
        "last_delivery_error": job.get("last_delivery_error"),
        "mark_job_run_drops": drops,
        "script": job.get("script"),
        "output_artifact": output,
        "repair_idempotency_key": f"mechanism-liveness:{exp.key}",
        "suggested_repair_title": f"REPAIR mechanism liveness: {exp.label}",
        **extra,
    }, exp)


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
