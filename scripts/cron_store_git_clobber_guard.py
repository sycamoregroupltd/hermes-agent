#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""Protect live cron scheduler state from git working-tree clobber.

ROOT CAUSE (t_3c33bc49, evidence in the task thread)
----------------------------------------------------
Every profile's live scheduler store ``profiles/<p>/cron/jobs.json`` is a
TRACKED file in the ``/home/frank/.hermes`` git repo. The scheduler writes
runtime state into it continuously (``last_run_at`` / ``last_status`` /
``next_run_at`` / ``fire_claim``) via ``cron.jobs.mark_job_run``.

Any git operation that rewrites the working tree from the index — ``git
checkout -- <path>``, ``git restore``, ``git reset --hard``, ``git stash`` —
silently reverts that live runtime state to whatever was last committed. It is
NOT a scheduler bug: ``mark_job_run`` wrote correctly, and a third party threw
the write away afterwards.

Observed signature (job e435af190e9e, upero-pm-governance):
  * live ``last_run_at`` frozen at 2026-07-29T06:06:58 for ~47h;
  * that exact value is what commit 30d458f (2026-07-29 06:11:30) holds;
  * output artifacts kept being written every 10m throughout;
  * the field "self-healed" the moment the job next fired and mark_job_run ran.
Also: 42 live jobs are currently missing the ``last_run_at`` KEY entirely —
the fingerprint of ``automation_vc_keeper.normalize_cron_json``, which strips
volatile scheduler fields before committing. A revert from such a commit does
not merely staled the field, it deletes it.

THE FIX
-------
Mark each live cron store ``--skip-worktree`` in the git index. Git then
refuses to overwrite the file on checkout/restore/reset --hard/stash, while
``git ls-files`` still lists it, so ``automation_vc_keeper`` discovery (which
copies from the WORKING TREE into a detached worktree) is unaffected.

Verified in a sandbox before shipping (see the task's clobber_proof.sh /
skipworktree_proof.sh): without the flag all four git operations clobber; with
the flag all four are refused or no-ops and ls-files still reports the path.

Usage
-----
    cron_store_git_clobber_guard.py            # check only (watchdog mode)
    cron_store_git_clobber_guard.py --apply    # set skip-worktree where missing
    cron_store_git_clobber_guard.py --revert   # clear the flag (undo)
    cron_store_git_clobber_guard.py --json     # machine-readable

Watchdog contract: prints NOTHING and exits 0 when every tracked live store is
protected (silent-when-healthy). Prints a report and exits 0 when unprotected
stores are found, so a no_agent cron delivers the alert without going red.
Exits 2 only on a real operational error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/frank/.hermes")


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO, text=True, capture_output=True, check=False
    )


def tracked_cron_stores() -> list[str]:
    """Tracked live cron stores, as repo-relative paths."""
    cp = git("ls-files", "cron/jobs.json", "profiles/*/cron/jobs.json")
    if cp.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {cp.stderr.strip()}")
    return sorted(line.strip() for line in cp.stdout.splitlines() if line.strip())


def skip_worktree_flags() -> dict[str, str]:
    """Map repo-relative path -> index flag letter from ``git ls-files -v``.

    'H' = normal (CLOBBERABLE), 'S' = skip-worktree (protected),
    'h'/'s' = assume-unchanged variants.
    """
    cp = git("ls-files", "-v", "cron/jobs.json", "profiles/*/cron/jobs.json")
    if cp.returncode != 0:
        raise RuntimeError(f"git ls-files -v failed: {cp.stderr.strip()}")
    out: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        flag, _, path = line.partition(" ")
        out[path.strip()] = flag.strip()
    return out


def is_protected(flag: str) -> bool:
    return flag in {"S", "s"}


def audit() -> dict:
    stores = tracked_cron_stores()
    flags = skip_worktree_flags()
    unprotected = [p for p in stores if not is_protected(flags.get(p, "H"))]
    protected = [p for p in stores if is_protected(flags.get(p, "H"))]
    return {
        "repo": str(REPO),
        "tracked_stores": len(stores),
        "protected": protected,
        "unprotected": unprotected,
        "healthy": not unprotected,
    }


def apply_flag(paths: list[str], on: bool) -> list[str]:
    """Set/clear skip-worktree. Returns paths that failed."""
    if not paths:
        return []
    flag = "--skip-worktree" if on else "--no-skip-worktree"
    failed = []
    for path in paths:
        cp = git("update-index", flag, path)
        if cp.returncode != 0:
            failed.append(f"{path}: {cp.stderr.strip()}")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser(description="Protect live cron stores from git clobber")
    ap.add_argument("--apply", action="store_true",
                    help="set skip-worktree on every unprotected tracked cron store")
    ap.add_argument("--revert", action="store_true",
                    help="clear skip-worktree on every protected tracked cron store")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if args.apply and args.revert:
        print("--apply and --revert are mutually exclusive", file=sys.stderr)
        return 2

    try:
        state = audit()
    except RuntimeError as exc:
        print(f"cron-store-git-clobber-guard ERROR: {exc}", file=sys.stderr)
        return 2

    changed: list[str] = []
    failed: list[str] = []
    if args.apply and state["unprotected"]:
        failed = apply_flag(state["unprotected"], on=True)
        changed = [p for p in state["unprotected"]
                   if p not in {f.split(":")[0] for f in failed}]
        state = audit()
    elif args.revert and state["protected"]:
        failed = apply_flag(state["protected"], on=False)
        changed = [p for p in state["protected"]
                   if p not in {f.split(":")[0] for f in failed}]
        state = audit()

    if args.json:
        print(json.dumps({**state, "changed": changed, "failed": failed},
                         indent=2, sort_keys=True))
        return 2 if failed else 0

    if failed:
        print("cron-store-git-clobber-guard: FAILED to update index for:")
        for f in failed:
            print(f"  {f}")
        return 2

    if changed:
        verb = "protected" if args.apply else "unprotected"
        print(f"cron-store-git-clobber-guard: {verb} {len(changed)} cron store(s):")
        for p in changed:
            print(f"  {p}")
        return 0

    if state["healthy"]:
        # Silent when healthy — watchdog contract.
        return 0

    print("# cron-store git-clobber exposure\n")
    print(f"{len(state['unprotected'])} live cron store(s) are TRACKED without "
          "`skip-worktree`. A `git checkout -- <path>` / `git restore` / "
          "`git reset --hard` / `git stash` in "
          f"{REPO} will silently revert their scheduler runtime state "
          "(`last_run_at`, `last_status`, `next_run_at`), which is the "
          "t_3c33bc49 last_run_at drift class.\n")
    for p in state["unprotected"]:
        print(f"  - {p}")
    print(f"\nRemediate: {Path(__file__).name} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
