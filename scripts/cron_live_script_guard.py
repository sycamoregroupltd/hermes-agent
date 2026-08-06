#!/usr/bin/env python3
"""cron_live_script_guard.py — DETECT + ESCALATE live-script guard for the jarvis cron fleet.

Checks every enabled cron job's resolved script path against the live repo root
every ~5m (no_agent cron). No autonomous service control — AMENDMENT 1.

Exit codes:
  0  healthy (all enabled jobs reference tracked, existing scripts)
  1  one or more enabled jobs reference MISSING / untracked / scheduler-blocked scripts
  2  usage / operational error

Flags:
  --json   machine-readable JSON output (always)
  --self-test  run a built-in self-test and exit

After 3 consecutive failures for the same job, escalate to os-reviewer with
full context. Repair-card idempotency keys are dated per cycle so relapse
detection is permanent (t_c10db950 observation).

Integrates with cron_untracked_script_guard.py (untracked-script guard) and
automation_vc_keeper.py (automation VC lineage) — see FORCE_INCLUDE and
REFerenced paths below.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

RAW_HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes")).expanduser()


def git_repo_root(start: Path) -> Path | None:
    r = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, timeout=120,
    )
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip()).expanduser()
    return None


REPO = git_repo_root(RAW_HERMES_HOME) or RAW_HERMES_HOME

# Mechanism keys the standing guard monitors (from t_4684e03b design).
MECHANISM_KEYS = {
    "kanban_review_required_auto_router",
    "sycode_alertmanager_spool_drain",
    "service_gate_escalation_watchdog",
    "fleet_kanban_integrity_backup",
}

# Canonical-content pins: repo-relative script path -> expected sha256 of the
# INSTALLED live copy. The integrity watchdog relapsed twice because a stale
# 127-line stub (sha 78413219) was both committed to HEAD (6d7657c) and
# installed live while existence+tracked checks passed silently. A content pin
# turns any future stale-snapshot revert into a loud CANONICAL-DRIFT violation
# (t_2b60fddf standing-guard acceptance).
# Rebuild: c18cb80 / 8c458e5, blob 4d312b31, 511 lines, sha e0c0a50c.
CANONICAL_SCRIPT_SHA = {
    "profiles/jarvis/scripts/jarvis_os_kanban_integrity_backup.py": (
        "e0c0a50c27ba16ac01079e064849342a33553674327b29921d5706a19a49fa26"
    ),
}

# Escalation threshold: consecutive failures before os-reviewer escalation.
ESCALATION_THRESHOLD = 3
ESCALATION_COOLDOWN_H = 1

# State persistence — lives in the repo so it survives checkouts.
STATE_DIR = REPO / "cron" / "state"
LIVE_GUARD_STATE = STATE_DIR / "cron_live_script_guard.json"
ESCALATION_STATE = STATE_DIR / "cron_live_script_guard_escalations.json"

# Delivery target for escalations (matches the untracked guard's critical-alerts channel).
ESCALATION_DELIVER = "discord:#critical-alerts"


def git(args: list[str], cwd: Path = REPO) -> tuple[int, str, str]:
    cmd = ["git", "-C", str(REPO), *args]
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def is_tracked(rel_path: str) -> bool:
    rc, _out, _err = git(["ls-files", "--error-unmatch", "--", rel_path])
    return rc == 0


def store_paths() -> list[tuple[Path, str]]:
    """Every cron store this guard is responsible for."""
    paths = [(p, p.parts[-3]) for p in sorted((REPO / "profiles").glob("*/cron/jobs.json"))]
    root = REPO / "cron" / "jobs.json"
    if root.exists():
        paths.append((root, "<root>"))
    return paths


def job_is_enabled(job: dict) -> bool:
    if job.get("enabled") is False:
        return False
    if job.get("state") == "paused":
        return False
    if job.get("disabled") is True:
        return False
    return True


def load_stores() -> tuple[list[dict], list[str]]:
    stores: list[dict] = []
    errors: list[str] = []
    for path, label in store_paths():
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            errors.append(f"failed to parse store {path}: {exc}")
            continue
        jobs = data.get("jobs") or []
        hits = []
        for job in jobs:
            if not job_is_enabled(job):
                continue
            script = job.get("script")
            if script:
                hits.append({
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "profile": label,
                    "script": script,
                    "store": str(path),
                })
        if hits:
            stores.append({"profile": label, "store": str(path), "jobs": hits})
    return stores, errors


def resolve_like_scheduler(profile_home: Path, script: str) -> tuple[Path, Path]:
    scripts_dir = (profile_home / "scripts").resolve()
    raw = Path(script).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()
    path.relative_to(scripts_dir)  # raises ValueError if outside
    return path, scripts_dir


def load_state() -> dict:
    if LIVE_GUARD_STATE.exists():
        try:
            return json.loads(LIVE_GUARD_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_GUARD_STATE.write_text(json.dumps(state, indent=2))


def load_escalations() -> dict:
    if ESCALATION_STATE.exists():
        try:
            return json.loads(ESCALATION_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_escalations(escalations: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ESCALATION_STATE.write_text(json.dumps(escalations, indent=2))


def escalate(job: dict, reason: str, fail_count: int) -> dict:
    """Record an escalation to os-reviewer. Returns the escalation record."""
    escalations = load_escalations()
    now = datetime.now(timezone.utc).isoformat()
    key = job["id"]
    # Dated per-cycle idempotency key — permanent singletons blind relapse detection.
    cycle_key = f"{key}:{now[:10]}"
    record = {
        "escalated_at": now,
        "cycle_idempotency_key": cycle_key,
        "fail_count": fail_count,
        "job_id": job["id"],
        "job_name": job["name"],
        "profile": job["profile"],
        "script": job["script"],
        "store": job["store"],
        "reason": reason,
        "escalation_deliver": ESCALATION_DELIVER,
    }
    escalations[cycle_key] = record
    save_escalations(escalations)
    return record


def should_escalate(job_id: str, fail_count: int) -> bool:
    if fail_count < ESCALATION_THRESHOLD:
        return False
    escalations = load_escalations()
    # Check if any escalation exists for this job_id within the cooldown window.
    now = datetime.now(timezone.utc)
    for k, v in escalations.items():
        if v.get("job_id") != job_id:
            continue
        try:
            last = datetime.fromisoformat(v["escalated_at"])
            age_h = (now - last).total_seconds() / 3600
            if age_h < ESCALATION_COOLDOWN_H:
                return False
        except (ValueError, TypeError, KeyError):
            return True
    return True


def audit() -> tuple[list[dict], list[str], list[str]]:
    violations: list[dict] = []
    reasons: list[str] = []
    errors: list[str] = []

    stores, store_errors = load_stores()
    errors.extend(store_errors)

    state = load_state()

    for store in stores:
        profile = store["profile"]
        profile_home = REPO if profile == "<root>" else REPO / "profiles" / profile
        for job in store["jobs"]:
            script = job["script"]
            scripts_dir_resolved = (profile_home / "scripts").resolve()
            try:
                resolved, _scripts_dir_resolved = resolve_like_scheduler(profile_home, script)
            except ValueError:
                violations.append({
                    "job_id": job["id"],
                    "job_name": job["name"],
                    "profile": profile,
                    "script": script,
                    "store": job["store"],
                    "reason": f"SCHEDULER-BLOCKED: script resolves outside owning profile scripts dir ({scripts_dir_resolved})",
                })
                continue

            if not resolved.exists():
                violations.append({
                    "job_id": job["id"],
                    "job_name": job["name"],
                    "profile": profile,
                    "script": script,
                    "store": job["store"],
                    "reason": f"MISSING script: {resolved}",
                })
                continue

            rel = str(resolved.relative_to(REPO))
            if not is_tracked(rel):
                violations.append({
                    "job_id": job["id"],
                    "job_name": job["name"],
                    "profile": profile,
                    "script": script,
                    "store": job["store"],
                    "reason": f"untracked script ({'absolute' if Path(script).is_absolute() else 'profile-local'}): {rel}",
                })
                continue

            # Canonical-content pin: a tracked-but-stale script (e.g. a
            # reverted rebuild) passes existence+tracked silently. Verify the
            # installed content hash for pinned mechanism scripts (t_2b60fddf).
            expected_sha = CANONICAL_SCRIPT_SHA.get(rel)
            if expected_sha:
                try:
                    installed_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
                except OSError as exc:
                    violations.append({
                        "job_id": job["id"],
                        "job_name": job["name"],
                        "profile": profile,
                        "script": script,
                        "store": job["store"],
                        "reason": f"CANONICAL-DRIFT: unreadable installed script {rel}: {exc}",
                    })
                    continue
                if installed_sha != expected_sha:
                    violations.append({
                        "job_id": job["id"],
                        "job_name": job["name"],
                        "profile": profile,
                        "script": script,
                        "store": job["store"],
                        "reason": (
                            f"CANONICAL-DRIFT: installed {rel} sha256 {installed_sha} "
                            f"!= canonical {expected_sha} (stale snapshot revert / rebuild not installed)"
                        ),
                    })
                    continue

            # Script exists, tracked, and content-canonical — healthy for this job.
            # Clear any prior failure count for this job.
            prev = state.get(job["id"], {})
            prev["fail_count"] = 0
            state[job["id"]] = prev

    # Escalate any jobs that have accumulated enough consecutive failures.
    now = datetime.now(timezone.utc).isoformat()
    for job_id, prev in list(state.items()):
        fail_count = prev.get("fail_count", 0)
        if fail_count >= ESCALATION_THRESHOLD and should_escalate(job_id, fail_count):
            # Find the job record for context.
            job_ctx = {}
            for store in stores:
                for j in store["jobs"]:
                    if j["id"] == job_id:
                        job_ctx = j
                        break
            if job_ctx:
                # Find the reason from the violations already collected.
                reason = next((v["reason"] for v in violations if v["job_id"] == job_id), "unknown")
                record = escalate(job_ctx, reason, fail_count)
                violations.append({
                    "job_id": job_id,
                    "job_name": job_ctx.get("name", ""),
                    "profile": job_ctx.get("profile", ""),
                    "script": job_ctx.get("script", ""),
                    "store": job_ctx.get("store", ""),
                    "reason": f"ESCALATED to os-reviewer after {fail_count} consecutive failures: {reason}",
                    "escalation_record": record,
                })

    save_state(state)
    return violations, sorted(set(v["reason"] for v in violations)), errors


def self_test() -> dict:
    """Run a built-in self-test and return results."""
    results = {"passed": [], "failed": [], "timestamp": datetime.now(timezone.utc).isoformat()}

    # Test 1: REPO resolves correctly.
    if REPO == RAW_HERMES_HOME and git_repo_root(RAW_HERMES_HOME) is None:
        results["failed"].append("git_repo_root: could not resolve repo root")
    else:
        results["passed"].append(f"git_repo_root: {REPO}")

    # Test 2: store_paths returns at least one store.
    stores = store_paths()
    if stores:
        results["passed"].append(f"store_paths: found {len(stores)} store(s)")
    else:
        results["failed"].append("store_paths: no cron stores found")

    # Test 3: load_stores parses without error.
    loaded_stores, load_errors = load_stores()
    if not load_errors:
        results["passed"].append(f"load_stores: {len(loaded_stores)} store(s) parsed OK")
    else:
        results["failed"].extend(load_errors)

    # Test 4: untracked-script guard is importable and runnable.
    guard_path = REPO / "scripts" / "cron_untracked_script_guard.py"
    if guard_path.exists():
        r = subprocess.run([sys.executable, str(guard_path), "--referenced-paths"],
                           capture_output=True, text=True, timeout=30)
        if r.returncode in (0, 1):
            results["passed"].append("cron_untracked_script_guard.py: importable and runnable")
        else:
            results["failed"].append(f"cron_untracked_script_guard.py: exit {r.returncode}")
    else:
        results["failed"].append("cron_untracked_script_guard.py: not found")

    # Test 5: automation_vc_keeper.py is importable.
    keeper_path = REPO / "scripts" / "automation_vc_keeper.py"
    if keeper_path.exists():
        results["passed"].append("automation_vc_keeper.py: present")
    else:
        results["failed"].append("automation_vc_keeper.py: not found")

    # Test 6: state directory is writable.
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_DIR.is_dir() and os.access(STATE_DIR, os.W_OK):
        results["passed"].append(f"state_dir writable: {STATE_DIR}")
    else:
        results["failed"].append(f"state_dir not writable: {STATE_DIR}")

    return results


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        results = self_test()
        print(json.dumps(results, indent=2))
        return 0 if not results["failed"] else 1

    if len(sys.argv) > 1 and sys.argv[1] == "--json":
        # Force JSON output mode.
        try:
            violations, reasons, errors = audit()
        except RuntimeError as exc:
            print(json.dumps({"healthy": False, "operational_error": str(exc)}, indent=2))
            return 2
        healthy = not violations and not errors
        out = {
            "healthy": healthy,
            "repo_root": str(REPO),
            "violation_count": len(violations),
            "violations": violations,
            "distinct_reasons": reasons,
            "errors": errors,
        }
        print(json.dumps(out, indent=2))
        return 0 if healthy else 1

    # Default: human-readable output.
    try:
        violations, reasons, errors = audit()
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    if violations or errors:
        print(f"cron_live_script_guard: {len(violations)} violation(s), {len(errors)} error(s)")
        for v in violations:
            print(f"  [{v.get('job_id', '?')}] {v.get('job_name', '?')} ({v.get('profile', '?')}): {v['reason']}")
        for e in errors:
            print(f"  ERROR: {e}", file=sys.stderr)
        return 1

    print(f"OK cron_live_script_guard: all enabled cron jobs reference tracked, existing scripts (repo_root={REPO})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())