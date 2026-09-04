#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Cron health meta-canary — no-agent cron.

Reads every profile-local ``~/.hermes/profiles/*/cron/jobs.json`` directly,
not the caller-scoped ``hermes cron list`` view. Silent when healthy. Emits a
single actionable alert when an enabled job has ``last_status=error`` (or a
delivery error), is overdue by more than 2x its interval/cron cadence, or is
UNPINNED — missing an explicit stable provider/model pin and therefore
susceptible to configuration drift or unintended fallback spend.

Drift detection (t_d43a2e82):
- Skips ``no_agent`` script-only jobs (they never consume a model).
- Skips jobs with an explicit job-level ``provider`` AND ``model`` (pinned
  jobs are stable by definition — silent even when they differ from baseline).
- Flags enabled LLM jobs that lack an explicit pin. The alert names the
  profile, job id/name, any last-run ``provider_snapshot``/``model_snapshot``
  evidence, the profile-default settings the job would resolve to, and whether
  that default differs from the approved global baseline (root config.yaml
  ``model.provider``/``model.default``).
- Dedupes profile stores by resolved realpath so a symlinked profile pair
  (e.g. ``sycode-trading -> sycode-trading-pm``) is scanned exactly once.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REAL_HERMES_HOME = Path(os.environ.get("HERMES_REAL_HOME", "/home/frank/.hermes")).expanduser()
if not (REAL_HERMES_HOME / "profiles").exists() and (REAL_HERMES_HOME / ".hermes" / "profiles").exists():
    REAL_HERMES_HOME = REAL_HERMES_HOME / ".hermes"
PROFILES_DIR = REAL_HERMES_HOME / "profiles"
MAX_ALERTS = int(os.environ.get("CRON_HEALTH_MAX_ALERTS", "25"))
# Approved global baseline: root config.yaml model.provider / model.default.
# Overridable for tests/simulation via CRON_HEALTH_BASELINE_CONFIG.
BASELINE_CONFIG = Path(
    os.environ.get("CRON_HEALTH_BASELINE_CONFIG", str(REAL_HERMES_HOME / "config.yaml"))
).expanduser()


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


def _load_model_settings(path: Path) -> tuple[str | None, str | None, str | None]:
    """Return (provider, default_model, error). Never raises."""
    try:
        import yaml
    except Exception as exc:
        return None, None, f"PyYAML unavailable: {exc}"
    try:
        data = yaml.safe_load(path.read_text()) or {}
        m = data.get("model") or {}
        return m.get("provider"), m.get("default"), None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


BASELINE_PROVIDER, BASELINE_MODEL, BASELINE_ERROR = _load_model_settings(BASELINE_CONFIG)


def _profile_model_defaults(profile: str) -> tuple[str | None, str | None, str | None]:
    """Profile-level config model defaults, falling back to the global baseline.

    Returns (provider, default_model, error). A profile without its own
    config.yaml resolves to the approved global baseline.
    """
    cfg = REAL_HERMES_HOME / "profiles" / profile / "config.yaml"
    if not cfg.exists():
        return BASELINE_PROVIDER, BASELINE_MODEL, None
    return _load_model_settings(cfg)


def profile_estop_active(profile_dir: Path) -> bool:
    """Is this profile's cron store under an active operator ESTOP?

    Mirrors cron_liveness_monitor.profile_estop_active (task t_6c2abedd):
    `hermes pause` writes a profile-local sentinel at the owning profile's
    HERMES_HOME (`~/.hermes/profiles/<p>/ESTOP`, plus any `*.ESTOP` variant,
    matching agent/estop.py's check_paused fail-safe). While the sentinel
    exists the cron scheduler SKIPS dispatching every job in that store, so
    OVERDUE/ERROR/DELIVERY flags for that store are expected state, not a
    health breach. Fail safe toward "paused" (an unreadable sentinel still
    counts as engaged).
    """
    candidates = [profile_dir / "ESTOP"] + sorted(profile_dir.glob("*.ESTOP"))
    return any(c.exists() for c in candidates)


def iter_profile_jobs() -> list[tuple[str, Path, dict[str, Any], bool]]:
    rows: list[tuple[str, Path, dict[str, Any], bool]] = []
    if not PROFILES_DIR.exists():
        rows.append(("<scan>", PROFILES_DIR, {"name": "<profiles>", "enabled": True, "last_status": "error", "last_error": f"profile cron root missing: {PROFILES_DIR}"}, False))
        return rows
    job_paths = sorted(PROFILES_DIR.glob("*/cron/jobs.json"))
    if not job_paths:
        rows.append(("<scan>", PROFILES_DIR, {"name": "<profiles>", "enabled": True, "last_status": "error", "last_error": f"zero profile cron stores matched under {PROFILES_DIR}"}, False))
        return rows
    seen_real: set[str] = set()
    for path in job_paths:
        # Dedupe symlinked profile stores (e.g. sycode-trading -> sycode-trading-pm)
        # so one physical jobs.json is scanned exactly once, under its real name.
        real = str(path.resolve())
        if real in seen_real:
            continue
        seen_real.add(real)
        profile_dir = path.parent.parent
        profile = profile_dir.name
        if profile_dir.is_symlink():
            profile = str(profile_dir.resolve()).rstrip("/").rsplit("/", 1)[-1]
        estop = profile_estop_active(profile_dir)
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            rows.append((profile, path, {"name": "<jobs.json>", "enabled": True, "last_status": "error", "last_error": f"unreadable jobs.json: {exc}"}, estop))
            continue
        for job in data.get("jobs", []):
            if isinstance(job, dict):
                rows.append((profile, path, job, estop))
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


def check_drift(profile: str, job: dict[str, Any]) -> list[str]:
    """Flag enabled LLM jobs that lack an explicit stable provider/model pin.

    Pinned jobs (explicit job-level ``provider`` AND ``model``) are stable by
    definition and stay silent, even when their pin differs from the approved
    global baseline (that difference is a deliberate, approved choice).

    Unpinned jobs get a descriptive alert naming the profile, job id/name,
    snapshot evidence (if any), the settings they would resolve to via the
    profile default, and whether that differs from the approved baseline.
    """
    if not job.get("enabled", True):
        return []
    if job.get("no_agent"):
        return []  # script-only job — never consumes a model
    if not job.get("id"):
        return []  # synthetic <scan>/<jobs.json> error rows are not real jobs
    name = job.get("name") or job.get("id") or "<unnamed>"
    jid = job.get("id") or "<no-id>"
    provider = job.get("provider")
    model = job.get("model")
    if provider is not None and model is not None:
        return []  # explicitly pinned — stable
    prefix = f"{profile}/{name} [{jid}]"
    p_snap = job.get("provider_snapshot")
    m_snap = job.get("model_snapshot")
    pp, pm, perr = _profile_model_defaults(profile)
    resolved = f"{pp or '<none>'}/{pm or '<none>'}"
    base = f"{BASELINE_PROVIDER or '<none>'}/{BASELINE_MODEL or '<none>'}"
    detail: list[str] = []
    if p_snap or m_snap:
        detail.append(f"last-run snapshot provider={p_snap or '<none>'}, model={m_snap or '<none>'}")
    if perr:
        detail.append(f"profile default unreadable: {perr}")
    if pp and pm and (pp != BASELINE_PROVIDER or pm != BASELINE_MODEL):
        detail.append(f"would resolve to profile default {resolved} — DIFFERS from approved baseline {base}")
    else:
        detail.append(f"would resolve to {resolved} (approved baseline {base})")
    snap_tag = " snapshot-recorded" if (p_snap or m_snap) else ""
    return [
        f"UNPINNED{snap_tag} {prefix}: no explicit provider/model pin (provider={provider!r}, model={model!r}); "
        + "; ".join(detail)
        + " — susceptible to config drift / unintended fallback spend"
    ]


def main() -> None:
    now = datetime.now(timezone.utc)
    bad: list[str] = []
    scanned_profiles: set[str] = set()
    scanned_jobs = 0

    if BASELINE_ERROR:
        bad.append(f"DRIFT-BASELINE {BASELINE_CONFIG}: cannot read approved global baseline: {BASELINE_ERROR}")

    suspended_profiles: set[str] = set()
    for profile, path, job, estop in iter_profile_jobs():
        scanned_profiles.add(profile)
        if estop:
            # Operator emergency stop (t_24425a94 / t_6c2abedd): the scheduler
            # intentionally skips dispatch for this store, so its jobs are
            # suspended, not broken. Count it as scanned, flag nothing.
            suspended_profiles.add(profile)
            continue
        if not job.get("enabled", True):
            continue
        # Tourniquet-paused jobs (paused_at set by operator/PM) are intentionally
        # not running; flagging them OVERDUE/ERROR churns the health key and
        # created false CRON-HEALTH cards (acradr pair, t_6de4fd95/t_b4409ee8,
        # observed 2026-08-28 — pause_reason documented but paused_at never landed).
        # Skip them like disabled jobs.
        if job.get("paused_at"):
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

        # Config-drift / unpinned-model detection (t_d43a2e82).
        bad.extend(check_drift(profile, job))

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
        # Show drift/unpinned alerts first: they are the proactive signal this
        # canary exists for and must not be truncated behind legacy ERROR/OVERDUE
        # noise when the total exceeds MAX_ALERTS. Detection counts are unchanged.
        bad.sort(key=lambda line: 0 if line.startswith(("UNPINNED", "DRIFT")) else 1)
        shown = bad[:MAX_ALERTS]
        lines = [f"🔴 CRON HEALTH: {len(bad)} issue(s) across {len(scanned_profiles)} profile cron store(s) ({len(suspended_profiles)} suspended by operator ESTOP), {scanned_jobs} enabled job(s) scanned"]
        lines.extend(f"  • {item}" for item in shown)
        if len(bad) > len(shown):
            lines.append(f"  • … {len(bad) - len(shown)} more")
        lines.append(f"Source: direct {PROFILES_DIR}/*/cron/jobs.json scan; silent when healthy.")
        print("\n".join(lines))
        # Exit 1 when findings exist so guard-bundle runner propagates the output
        # to the kanban router (t_a45e23da). The wrapper routes non-empty stdout
        # to cron_health_kanban_router.py, but the bundle runner only preserves
        # output when the wrapper's rc != 0. Previously this always exited 0,
        # so ERROR lines were suppressed before reaching the consumer.
        sys.exit(1)


if __name__ == "__main__":
    main()
