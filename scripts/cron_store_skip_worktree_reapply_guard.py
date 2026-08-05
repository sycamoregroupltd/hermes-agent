#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""Periodic re-enforce + alert wrapper for cron-store untracked protection.

LEGACY NAME, CURRENT SEMANTICS (t_6c32b13c, completed 2026-08-05): the
skip-worktree approach this wrapper was named for did NOT survive reset/rebase
index rewrites and was replaced by the structural model — live cron stores are
UNTRACKED + GITIGNORED, with sanitized recovery snapshots under the non-live
``cron-snapshots/`` path. The canonical ``cron_store_git_clobber_guard.py``
now enforces "no store is ever tracked" (untrack index + commit the untracking
on HEAD); this wrapper simply runs it in --apply mode and alerts
discord:#critical-alerts on any repair, so the t_3c33bc49 git-clobber class
cannot silently reopen after a checkout/reset to a historical commit.

The 2m ``fleet_cron_store_clobber_canary`` performs the same check; this
wrapper is kept for compatibility with existing references and as a manual
re-enforcement entry point.

Returns 1 on a repair event (so the ticker records it), 2 only on a genuine
operational error, 0 when healthy.

A2/A3-safe: bounded git index/ref ops + alert only; never mutates schedules,
store content, provider/model routing, credentials, or live-trading.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REAL_HERMES_HOME = Path(os.environ.get("HERMES_REAL_HOME", "/home/frank/.hermes")).expanduser()
if not (REAL_HERMES_HOME / "profiles").exists() and (REAL_HERMES_HOME / ".hermes" / "profiles").exists():
    REAL_HERMES_HOME = REAL_HERMES_HOME / ".hermes"

SCRIPTS_DIR = REAL_HERMES_HOME / "scripts"
GUARD = SCRIPTS_DIR / "cron_store_git_clobber_guard.py"
ALERT_TARGET = os.environ.get("CLOBBER_ALERT_TARGET", "discord:#critical-alerts")
HERMES = os.environ.get("HERMES_BIN", "hermes")


def send_alert(message: str) -> tuple[bool, str]:
    """Deliver to discord:#critical-alerts; reuse blocked_task_notifier if
    importable, else a minimal hermes-send fallback so the guard never crashes."""
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
            [HERMES, "send", "-q", "-t", ALERT_TARGET,
             "-s", "cron-store skip-worktree reapply guard", message],
            capture_output=True, text=True, timeout=60, env=env,
        )
        return result.returncode == 0, f"rc={result.returncode}"
    except Exception as exc:  # never raise into main()
        return False, f"alert-exception:{type(exc).__name__}"


def run_guard(extra: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), "--json", *extra],
        capture_output=True, text=True, timeout=120,
    )


def main() -> int:
    # Audit + apply in one shot (--apply re-protects any unprotected store,
    # including a freshly tracked new-profile store or a re-cloned checkout).
    r = run_guard(["--apply"])
    if r.returncode == 2 and not r.stdout.strip():
        # Operational error (e.g. git ls-files failed) — surface + let ticker mark error.
        print("cron-store-skip-worktree-reapply-guard ERROR:", r.stderr.strip(), file=sys.stderr)
        return 2

    try:
        state = json.loads(r.stdout)
    except Exception:
        # Non-JSON guard output (shouldn't happen with --json); fall back to audit-only.
        r2 = run_guard([])
        try:
            state = json.loads(r2.stdout)
        except Exception:
            print("cron-store-skip-worktree-reapply-guard ERROR: guard produced no JSON",
                  file=sys.stderr)
            print(r.stdout, file=sys.stderr)
            return 2

    changed = state.get("changed", [])
    failed = state.get("failed", [])

    if changed or failed:
        lines = ["[cron-store-untracked-reapply-guard] re-enforced untracked cron-store protection "
                 f"@ {os.environ.get('HOSTNAME', 'dgx')}"]
        if changed:
            lines.append(f"  repair action(s) ({len(changed)}):")
            for p in changed:
                lines.append(f"    + {p}")
        if failed:
            lines.append(f"  FAILED repair action(s) ({len(failed)}):")
            for f in failed:
                lines.append(f"    ! {f}")
        lines.append(
            "  Cause: a checkout/reset/rebase to a historical commit re-tracked live cron "
            "stores (the t_3c33bc49/t_6c32b13c git-clobber class was re-opened locally). "
            "The guard untracked them and committed the untracking on HEAD. If store "
            "content was clobbered, gateways self-heal by deferring one interval — check "
            "profiles/*/logs/agent.log for 'had no next_run_at' and the reflog for the "
            "triggering operation."
        )
        msg = "\n".join(lines)
        ok, detail = send_alert(msg)
        print(msg + f" [alert={ok}:{detail}]")
        return 1

    # Healthy: silent when unprotected count is 0.
    return 0


if __name__ == "__main__":
    sys.exit(main())
