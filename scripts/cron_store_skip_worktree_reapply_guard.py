#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""Periodic re-apply + alert guard for cron-store skip-worktree protection.

This is the FIELD-LEVEL / UNPROTECTED-STORE complement to
``fleet_cron_store_clobber_canary.py`` (which only detects 0-job / deploy-race
clobbers). The skip-worktree bit set by ``cron_store_git_clobber_guard.py
--apply`` is a LOCAL, per-clone git index flag. It is NOT committed and does NOT
survive:
  (a) a fresh ``git clone`` of the fleet-automation repo,
  (b) an index rebuild / ``.git/index`` loss,
  (c) a NEWLY CREATED profile whose scheduler store is added to tracking after
      the one-shot ``--apply`` ran.

This guard runs silently when healthy. On every tick it runs the canonical
guard in audit + apply mode across all tracked scheduler stores; if any store
was unprotected (fresh clone, index loss, or a new profile), it re-applies the
protection AND alerts discord:#critical-alerts so the t_3c33bc49 git-clobber
class cannot silently reopen.

Design mirrors the sibling canary (silent when healthy, self-alerts on event,
deliver=local on the job so the ticker does not double-deliver). Returns 1 on a
re-protection event (so the ticker records it), 2 only on a genuine operational
error, 0 when healthy.

A2/A3-safe: bounded git index ops (update-index --skip-worktree) + alert only;
never mutates schedules, provider/model routing, credentials, or live-trading.
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
        lines = ["[cron-store-skip-worktree-reapply-guard] re-applied skip-worktree protection "
                 f"@ {os.environ.get('HOSTNAME', 'dgx')}"]
        if changed:
            lines.append(f"  re-protected {len(changed)} store(s):")
            for p in changed:
                lines.append(f"    + {p}")
        if failed:
            lines.append(f"  FAILED to protect {len(failed)} store(s):")
            for f in failed:
                lines.append(f"    ! {f}")
        lines.append(
            "  Cause: a freshly-cloned/rebuilt checkout or a newly-tracked profile store had "
            "no skip-worktree bit (the t_3c33bc49 git-clobber class was re-opened locally). "
            "Re-apply closed it. Verify no concurrent git working-tree rewrite (checkout/restore/"
            "reset --hard/stash) is in flight that would re-revert scheduler runtime state."
        )
        msg = "\n".join(lines)
        ok, detail = send_alert(msg)
        print(msg + f" [alert={ok}:{detail}]")
        return 1

    # Healthy: silent when unprotected count is 0.
    return 0


if __name__ == "__main__":
    sys.exit(main())
