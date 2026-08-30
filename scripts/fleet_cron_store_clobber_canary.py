#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Deploy-atomicity / cron-store-clobber regression guard (t_838b8d66 #3).

Closes the recurring deploy-race cron clobber class (recurred 07-14/15/17):
a fleet deploy/migration rewrites profile cron stores and scripts in place
without holding cron or staging the swap, so (a) a running/pending 1m job
races a missing script and (b) stores are reset to empty.

This canary runs every 2 minutes, silent when healthy. It:
  1. Detects a profile cron store that has 0 jobs but a sibling
     ``jobs.json.bak-migrate-*`` / ``jobs.json.bak-*`` with non-empty jobs
     taken within the deploy window (~10 min) -> event CRON_STORE_CLOBBER:
     atomic-restore from the latest non-empty backup + alert discord:#critical-alerts.
  2. Watchdog-script guard: asserts ``elon_kanban_watchdog.py`` is present in
     $HERMES_HOME/scripts; if missing -> WATCHDOG_SCRIPT_VANISHED alert. Also
     asserts the 1m watchdog job's ``script`` file resolves (global or
     profile-local).
  3. Git-tracking guard (t_6c32b13c completion, 2026-08-05 outage): live cron
     stores must NEVER be tracked by git — a tracked sanitized copy clobbers
     next_run_at on every checkout/reset and silently defers every job. Runs
     cron_store_git_clobber_guard.py --apply (untrack index + commit the
     untracking on HEAD) and alerts on any repair. This is the scheduled
     backstop for ``git reset --hard <historical-commit>``, which fires no
     git hook.

Reuses dgx_cron_health_canary.py scanning conventions and
blocked_task_notifier.send_alert() for delivery. No hermes-core changes.
A2 / A3-safe: bounded file ops + git index/ref repair + alert only; never
mutates schedules, store content, provider/model routing, credentials, or
live-trading.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REAL_HERMES_HOME = Path(os.environ.get("HERMES_REAL_HOME", "/home/frank/.hermes")).expanduser()
if not (REAL_HERMES_HOME / "profiles").exists() and (REAL_HERMES_HOME / ".hermes" / "profiles").exists():
    REAL_HERMES_HOME = REAL_HERMES_HOME / ".hermes"

PROFILES_DIR = REAL_HERMES_HOME / "profiles"
SCRIPTS_DIR = REAL_HERMES_HOME / "scripts"
GLOBAL_CRON_STORE = REAL_HERMES_HOME / "cron" / "jobs.json"

# A store is considered "just clobbered" if its most recent non-empty backup was
# taken within this window of the now-empty store's updated_at (deploy race).
CLOBBER_WINDOW_SECONDS = int(os.environ.get("CLOBBER_WINDOW_SECONDS", "600"))

ALERT_TARGET = os.environ.get("CLOBBER_ALERT_TARGET", "discord:#critical-alerts")
HERMES = os.environ.get("HERMES_BIN", "hermes")


def send_alert(message: str) -> tuple[bool, str]:
    """Deliver to discord:#critical-alerts (named Frank-critical consumer).

    Imported from blocked_task_notifier if importable; otherwise a minimal
    hermes-send fallback so the guard never crashes on import failure.
    """
    try:
        sys.path.insert(0, str(SCRIPTS_DIR))
        from blocked_task_notifier import send_alert as _send  # type: ignore
        return _send(message)
    except Exception:
        pass
    env = os.environ.copy()
    env["HERMES_HOME"] = str(REAL_HERMES_HOME)
    try:
        result = subprocess.run(
            [HERMES, "send", "-q", "-t", ALERT_TARGET, "-s", "fleet cron-store clobber guard", message],
            capture_output=True, text=True, timeout=60, env=env,
        )
        return result.returncode == 0, f"rc={result.returncode}"
    except Exception as exc:  # never raise into main()
        return False, f"alert-exception:{type(exc).__name__}"


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def store_paths() -> list[Path]:
    paths: list[Path] = []
    if PROFILES_DIR.exists():
        seen: set[str] = set()
        for p in sorted(PROFILES_DIR.glob("*/cron/jobs.json")):
            real = str(p.resolve())
            if real in seen:
                continue
            seen.add(real)
            paths.append(p)
    if GLOBAL_CRON_STORE.exists():
        paths.append(GLOBAL_CRON_STORE)
    return paths


def latest_nonempty_backup(store: Path) -> Optional[tuple[Path, list[dict[str, Any]], datetime]]:
    """Return (backup_path, jobs, backup_mtime) for the most recent non-empty
    backup of ``store`` within the clobber window, else None."""
    store_mtime = datetime.fromtimestamp(store.stat().st_mtime, tz=timezone.utc)
    candidates = sorted(store.parent.glob("jobs.json.bak*"), reverse=True)
    best: Optional[tuple[Path, list[dict[str, Any]], datetime]] = None
    for bak in candidates:
        if not bak.is_file():
            continue
        try:
            data = json.loads(bak.read_text())
        except Exception:
            continue
        jobs = [j for j in data.get("jobs", []) if isinstance(j, dict)]
        if not jobs:
            continue
        bak_mtime = datetime.fromtimestamp(bak.stat().st_mtime, tz=timezone.utc)
        # Only treat as a deploy-race clobber if the backup is close in time to
        # the now-empty store (a deploy/migration pass), not an old manual backup.
        if abs((store_mtime - bak_mtime).total_seconds()) > CLOBBER_WINDOW_SECONDS:
            continue
        cand = (bak, jobs, bak_mtime)
        if best is None or bak_mtime > best[2]:
            best = cand
    return best


def atomic_restore(store: Path, backup: Path) -> tuple[bool, str]:
    """Restore store from backup atomically (write temp, fsync, rename)."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety = store.with_suffix(f".bak-clobberguard-{ts}")
        if store.exists():
            shutil.copy2(store, safety)
        tmp = store.with_suffix(".tmp-restore")
        shutil.copy2(backup, tmp)
        os.replace(tmp, store)
        return True, str(safety)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def check_store_clobber(store: Path) -> Optional[str]:
    try:
        data = json.loads(store.read_text())
    except Exception as exc:
        return f"CLOBBER-GUARD-UNREADABLE {store}: {exc}"
    jobs = [j for j in data.get("jobs", []) if isinstance(j, dict)]
    if jobs:
        return None  # healthy: has jobs, nothing to do
    # Empty store -> look for a recent non-empty backup (deploy-race signature).
    bak = latest_nonempty_backup(store)
    if bak is None:
        return None  # genuinely empty and no recent backup; not a clobber
    backup_path, bak_jobs, bak_mtime = bak
    ok, detail = atomic_restore(store, backup_path)
    profile = store.parts[-3] if "profiles" in store.parts else "global"
    if ok:
        msg = (
            f"CRON_STORE_CLOBBER RECOVERED {profile}: store {store} had 0 jobs but "
            f"backup {backup_path.name} (taken {bak_mtime.isoformat()}) held "
            f"{len(bak_jobs)} job(s). Atomic-restored from backup; safety copy {detail}. "
            f"A fleet deploy clobbered this store — verify the deploy path holds cron "
            f"during store replacement (t_838b8d66 #3)."
        )
    else:
        msg = (
            f"CRON_STORE_CLOBBER UNRECOVERED {profile}: store {store} has 0 jobs and "
            f"recent backup {backup_path.name} holds {len(bak_jobs)} job(s) but restore "
            f"failed: {detail}. MANUAL ACTION REQUIRED."
        )
    ok_alert, detail_alert = send_alert(msg)
    return f"{msg} [alert={ok_alert}:{detail_alert}]"


def check_watchdog_script() -> Optional[str]:
    """The 1m CEO-loop watchdog must survive a deploy that removes scripts."""
    wd = SCRIPTS_DIR / "elon_kanban_watchdog.py"
    if wd.exists():
        return None
    msg = (
        "WATCHDOG_SCRIPT_VANISHED: /home/frank/.hermes/scripts/elon_kanban_watchdog.py "
        "is missing — the 1m CEO-loop BUSY/READY signal source is gone (deploy-race). "
        "Restore from a non-empty backup or re-create. A future deploy must not remove "
        "this script without alerting (t_838b8d66 #3)."
    )
    ok, detail = send_alert(msg)
    return f"{msg} [alert={ok}:{detail}]"


def check_watchdog_job_resolution() -> Optional[str]:
    """Assert the 1m watchdog job's script file resolves (global or profile-local)."""
    if not GLOBAL_CRON_STORE.exists():
        return None
    try:
        data = json.loads(GLOBAL_CRON_STORE.read_text())
    except Exception:
        return None
    for job in data.get("jobs", []):
        if not isinstance(job, dict):
            continue
        if job.get("id") != "e97a2e581fa8" and "watchdog" not in str(job.get("name", "")).lower():
            continue
        script = job.get("script")
        if not script:
            continue
        raw = Path(script)
        if raw.is_absolute():
            continue
        if (SCRIPTS_DIR / raw).exists():
            return None
        # search profile-local scripts dirs
        found = any((PROFILES_DIR / p / "scripts" / raw).exists() for p in os.listdir(PROFILES_DIR))
        if found:
            return None
        msg = (
            f"WATCHDOG_SCRIPT_UNRESOLVED: 1m watchdog job {job.get('id')} references "
            f"script '{script}' that does not resolve in global or any profile-local "
            f"scripts/ dir — will fail on next tick (deploy-race)."
        )
        ok, detail = send_alert(msg)
        return f"{msg} [alert={ok}:{detail}]"
    return None


def check_store_git_tracking(dry_run: bool) -> Optional[str]:
    """Live stores must never be tracked by git (t_6c32b13c completion).

    Delegates to the canonical cron_store_git_clobber_guard.py so there is one
    implementation of the untrack/commit repair. Silent when healthy."""
    guard = SCRIPTS_DIR / "cron_store_git_clobber_guard.py"
    if not guard.exists():
        msg = ("CRON_STORE_GIT_GUARD_MISSING: scripts/cron_store_git_clobber_guard.py "
               "is gone — the tracked-store invariant is unenforced.")
        ok, detail = send_alert(msg)
        return f"{msg} [alert={ok}:{detail}]"
    cmd = [sys.executable, str(guard), "--json"]
    if not dry_run:
        cmd.append("--apply")
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        state = json.loads(cp.stdout)
    except Exception as exc:
        return f"CRON_STORE_GIT_GUARD_ERROR: {type(exc).__name__}: {str(exc)[:200]}"
    changed = state.get("changed", [])
    failed = state.get("failed", [])
    if state.get("healthy") and not changed and not failed:
        return None
    msg = (
        f"CRON_STORE_GIT_TRACKING {'DETECTED (dry-run)' if dry_run else 'REPAIRED'}: "
        f"live cron store(s) were tracked by git in {state.get('repo')} — a "
        f"checkout/reset re-introduced them (index={len(state.get('index_tracked', []))}, "
        f"HEAD={len(state.get('head_tracked', []))} remaining). "
        f"Repairs: {changed or 'none'}. Failures: {failed or 'none'}. "
        "NOTE: if git just clobbered store content, gateways self-heal by "
        "deferring one interval ('had no next_run_at' in agent.log); check "
        "profiles/*/logs/agent.log and the reflog for the triggering operation "
        "(t_6c32b13c / 2026-08-05 outage class)."
    )
    ok, detail = send_alert(msg)
    return f"{msg} [alert={ok}:{detail}]"


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="fleet cron-store clobber guard")
    parser.add_argument("--dry-run", action="store_true",
                        help="detect + alert only; never restore or mutate stores")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    findings: list[str] = []

    # 1. cron-store clobber detection + auto-restore
    for store in store_paths():
        f = check_store_clobber(store)
        if f:
            findings.append(f)

    # 2. watchdog-script presence + resolution guards
    f = check_watchdog_script()
    if f:
        findings.append(f)
    f = check_watchdog_job_resolution()
    if f:
        findings.append(f)

    # 3. live stores must never be tracked by git (untrack + commit repair)
    f = check_store_git_tracking(args.dry_run)
    if f:
        findings.append(f)

    if findings:
        summary = (
            f"[fleet_cron_store_clobber_canary] {len(findings)} deploy-race finding(s) @ "
            f"{now.isoformat()}:\n" + "\n".join(f"  - {x}" for x in findings)
        )
        # Emit to stdout for the cron job artifact/log and re-alert via discord.
        print(summary)
        send_alert(summary)
        return 1

    # Healthy: silent.
    return 0


if __name__ == "__main__":
    sys.exit(main())
