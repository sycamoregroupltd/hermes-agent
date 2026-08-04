#!/usr/bin/env python3
"""live_root_integrity_guard.py — detect when a worker's git operation guts the live root.

WHY (incident 2026-08-04, twice within one hour):
  ~/.hermes is BOTH a git repo and the live cron execution root. A worker ran
  `git reset --hard origin/fleet/automation-vc`, moving HEAD off the branch several
  live scripts were committed on. Six scripts vanished from the working tree and three
  cron jobs disappeared from the store. Independently, a `git checkout` in
  /home/frank/sycode-trading reverted an uncommitted docker-compose.yml, undoing a
  network security fix. Neither was noticed by anything — both were found by accident.

  The existing fleet-cron-store-clobber-canary could not have caught it: its script
  mentions `git` zero times, and its own cron job had been wiped as well. A guard that
  is itself deleted by the failure it guards against is not a guard.

WHAT IT CHECKS (all cheap, read-only):
  1. MANIFEST  — every critical script still exists and is non-empty.
  2. CRON      — every critical cron job is still registered.
  3. HEAD      — the live root's git HEAD, versus the last observed value. A move is
                 not itself an error, but it is the mechanism, so it is reported
                 alongside anything found missing to make the cause obvious.

FAIL-CLOSED: probe errors exit non-zero. A no-agent cron's only liveness signal is its
exit code, so an unreadable manifest must never read as "everything present".
Healthy = empty stdout, exit 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERMES = Path("/home/frank/.hermes")
STATE = HERMES / "var" / "live-root-integrity.json"

# Scripts whose disappearance is silent and consequential.
CRITICAL_SCRIPTS = [
    "profiles/jarvis/scripts/nous_storm_recovery.py",
    "profiles/jarvis/scripts/pr_review_health_monitor.py",
    "profiles/jarvis/scripts/pr-review-lane-tick.sh",
    "profiles/jarvis/scripts/prune_fallback_chain.py",
    "profiles/jarvis/scripts/set_fallback_chain.py",
    "profiles/jarvis/scripts/check_provider_health.py",
    "profiles/jarvis/scripts/kanban_transient_recovery.py",
    "scripts/review-lane-tick.sh",
    "scripts/merge-train.sh",
]

CRITICAL_CRONS = [
    "codex-exhaustion-circuit-breaker",
    "board-unroutable-assignee-sweep",
    "capability-routing-guard",
    "live-root-integrity-guard",
    "nous-storm-recovery",
    "pr-review-health-monitor",
    "pr-review-lane",
    "review-work-lane",
    "fleet-dispatch-loop",
]


class ProbeError(RuntimeError):
    pass


def git_head(repo: Path) -> str:
    try:
        cp = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, timeout=30)
        br = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                            capture_output=True, text=True, timeout=30)
        if cp.returncode != 0:
            raise ProbeError(f"git rev-parse failed in {repo}: {cp.stderr.strip()[:120]}")
        return f"{cp.stdout.strip()}@{br.stdout.strip() or 'DETACHED'}"
    except subprocess.SubprocessError as e:
        raise ProbeError(f"git probe failed in {repo}: {e}")


def registered_crons() -> tuple[set[str], set[str]]:
    """Return (enabled_names, present_but_disabled_names).

    `hermes cron list` prints ONLY enabled jobs — 106 of 194 on this host. Treating
    its output as "exists" reports a merely-DISABLED job as MISSING, which is a
    false alarm that trains the reader to ignore the guard. Read the store directly
    so absence and disablement are distinguishable; they need different fixes
    (recreate vs `hermes cron resume <id>`).
    """
    store = HERMES / "profiles" / "jarvis" / "cron" / "jobs.json"
    if not store.is_file():
        raise ProbeError(f"cron store missing: {store}")
    try:
        raw = json.loads(store.read_text(errors="replace"))
    except (OSError, json.JSONDecodeError) as e:
        raise ProbeError(f"cron store unreadable: {e}")
    jobs = raw if isinstance(raw, list) else raw.get("jobs", raw)
    jobs = jobs if isinstance(jobs, list) else list(jobs.values())
    enabled, disabled = set(), set()
    for j in jobs:
        if not isinstance(j, dict):
            continue
        name = j.get("name") or ""
        if not name:
            continue
        (enabled if j.get("enabled", True) else disabled).add(name)
    return enabled, disabled


def main() -> int:
    missing_scripts = [s for s in CRITICAL_SCRIPTS
                       if not (HERMES / s).is_file() or (HERMES / s).stat().st_size == 0]

    crons, disabled_crons = registered_crons()
    missing_crons = [c for c in CRITICAL_CRONS if c not in crons and c not in disabled_crons]
    # Duplicate job entries are common (re-registration leaves the old row behind), so a
    # name can appear in BOTH sets. One enabled entry is enough for the job to fire —
    # only flag names with no enabled entry at all, or the guard cries wolf on every dupe.
    inactive_crons = [c for c in CRITICAL_CRONS if c in disabled_crons and c not in crons]

    head = git_head(HERMES)
    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except (OSError, json.JSONDecodeError):
            prev = {}
    moved = prev.get("head") and prev["head"] != head

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"head": head, "cron_count": len(crons)}, indent=2))

    if not missing_scripts and not missing_crons and not inactive_crons:
        return 0  # healthy -> silent

    print(f"LIVE ROOT INTEGRITY — {HERMES}")
    if missing_scripts:
        print(f"  MISSING SCRIPTS ({len(missing_scripts)}):")
        for s in missing_scripts:
            print(f"    - {s}")
    if missing_crons:
        print(f"  MISSING CRON JOBS — absent from the store, must be recreated ({len(missing_crons)}):")
        for c in missing_crons:
            print(f"    - {c}")
    if inactive_crons:
        print(f"  DISABLED CRON JOBS — present but will never fire ({len(inactive_crons)}):")
        for c in inactive_crons:
            print(f"    - {c}   (fix: hermes cron resume <job-id>)")
    print(f"  git HEAD now: {head}" + (f"  (MOVED from {prev.get('head')})" if moved else ""))
    print(f"  registered crons: {len(crons)}" +
          (f"  (was {prev['cron_count']})" if prev.get("cron_count") else ""))
    if moved:
        print("  LIKELY CAUSE: a worker ran a git checkout/reset in the live execution root.")
        print("  Recover: git log --all --oneline | grep <script>, then git checkout <sha> -- <path>")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as e:
        print(f"live_root_integrity_guard: PROBE FAILED (not 'healthy'): {e}", file=sys.stderr)
        sys.exit(1)
