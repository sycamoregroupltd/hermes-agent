#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies.
"""Enforce that live cron scheduler stores are NEVER tracked by git.

ROOT CAUSE (t_3c33bc49, t_6c32b13c; 2026-08-05 vault-autocommit outage)
-----------------------------------------------------------------------
Every profile's live scheduler store ``profiles/<p>/cron/jobs.json`` (plus the
root ``cron/jobs.json``) is mutable runtime state: the scheduler continuously
writes ``next_run_at`` / ``last_run_at`` / ``fire_claim`` into it. The repo's
history, however, contains SANITIZED copies (``automation_vc_keeper.
normalize_cron_json`` strips those fields before committing). While those
copies stayed tracked, ANY ``git checkout`` / ``git reset --hard`` / ``git
rebase`` in /home/frank/.hermes rewrote every live store with the sanitized
copy; each gateway then logged a burst of "Job '<name>' had no next_run_at;
recovering interval run at <now+interval>" and re-deferred EVERY job by a full
interval. Repeated clobbers meant no cron job fired anywhere, silently
(2026-08-05 11:15-11:55 bursts).

The earlier skip-worktree defence did NOT survive index rewrites (reset /
rebase cleared the bits; observed empirically on 2026-08-05 — all 27 stores
were back to 'H'), so protection is now STRUCTURAL:

THE MODEL (completes t_6c32b13c)
--------------------------------
1. Live stores are UNTRACKED and GITIGNORED (``/cron/*`` and
   ``/profiles/*/cron/*`` deny rules in .gitignore). A tree that does not
   track a path cannot clobber it.
2. Sanitized recovery snapshots live under the non-live path
   ``cron-snapshots/`` on the automation branch (written by
   automation_vc_keeper).
3. THIS guard enforces invariant 1 on the live repo. A checkout / reset to a
   pre-untracking (historical) commit re-tracks the stores — and, because the
   live files are gitignored, git treats them as expendable and SILENTLY
   OVERWRITES them with the sanitized copies. The guard repairs the git state
   so the very next git operation cannot clobber again:
     * removes any store from the index (``git update-index --force-remove``);
     * if HEAD's tree still carries stores, commits an untracking commit ON
       TOP of HEAD via a temporary index (never touching the real index or
       working tree), so ``git reset --hard HEAD`` cannot re-track them.
       The auto-commit is skipped while a rebase/merge/cherry-pick/bisect is
       in progress (reported in ``failed`` instead).
   Store CONTENT lost to such a historical clobber is not restored here — the
   gateway self-heals (one-interval deferral) and the alert from the caller
   (fleet_cron_store_clobber_canary / post-checkout hook log) makes it loud.

Invoked from:
  * fleet_cron_store_clobber_canary.py (every 2m, jarvis) — scheduled backstop
    that also covers ``git reset --hard`` (which fires no hook);
  * .git/hooks/post-checkout (immediate repair after any checkout);
  * cron_store_skip_worktree_reapply_guard.py (legacy wrapper, kept for
    compatibility — same --apply --json contract).

Usage
-----
    cron_store_git_clobber_guard.py            # audit only
    cron_store_git_clobber_guard.py --apply    # untrack + commit where needed
    cron_store_git_clobber_guard.py --json     # machine-readable
    cron_store_git_clobber_guard.py --quiet    # with --json: print nothing
                                               # when healthy and unchanged

Watchdog contract: exits 0 and (with --quiet) prints nothing when no store is
tracked in the index or in HEAD's tree. Exits 2 only on operational error.
A2/A3-safe: bounded git index/ref ops only; never mutates schedules, store
content, provider/model routing, credentials, or live-trading.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("HERMES_LIVE_REPO", "/home/frank/.hermes"))

STORE_GLOBS = ("cron/jobs.json", "profiles/*/cron/jobs.json")

UNTRACK_COMMIT_MESSAGE = (
    "fix(cron): untrack live cron stores re-introduced by a historical checkout/reset\n"
    "\n"
    "Auto-committed by cron_store_git_clobber_guard --apply (t_6c32b13c). Live\n"
    "profiles/*/cron/jobs.json + cron/jobs.json are runtime scheduler state and\n"
    "must never be tracked: a tracked sanitized copy clobbers next_run_at on\n"
    "every checkout/reset and silently defers the whole fleet's cron jobs."
)


def git(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO, text=True, capture_output=True, check=False, env=env
    )


def is_store_path(rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, g) for g in STORE_GLOBS)


def index_tracked_stores() -> list[str]:
    """Live cron stores currently tracked in the index (repo-relative)."""
    cp = git("ls-files", "--", *STORE_GLOBS)
    if cp.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {cp.stderr.strip()}")
    return sorted(line.strip() for line in cp.stdout.splitlines() if line.strip())


def head_tracked_stores() -> list[str]:
    """Live cron stores present in HEAD's tree (repo-relative)."""
    cp = git("ls-tree", "-r", "--name-only", "HEAD")
    if cp.returncode != 0:
        # An unborn HEAD (fresh repo) has no tree — treat as empty, not an error.
        if "Not a valid object name" in cp.stderr or "unknown revision" in cp.stderr:
            return []
        raise RuntimeError(f"git ls-tree HEAD failed: {cp.stderr.strip()}")
    return sorted(
        line.strip() for line in cp.stdout.splitlines()
        if line.strip() and is_store_path(line.strip())
    )


def git_op_in_progress() -> str | None:
    """Name of an in-flight history-rewriting operation, or None."""
    cp = git("rev-parse", "--git-dir")
    if cp.returncode != 0:
        raise RuntimeError(f"git rev-parse --git-dir failed: {cp.stderr.strip()}")
    gitdir = Path(cp.stdout.strip())
    if not gitdir.is_absolute():
        gitdir = REPO / gitdir
    for marker, name in (
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ):
        if (gitdir / marker).exists():
            return name
    return None


def audit() -> dict:
    idx = index_tracked_stores()
    head = head_tracked_stores()
    return {
        "repo": str(REPO),
        "index_tracked": idx,
        "head_tracked": head,
        "tracked_stores": len(idx),
        "healthy": not idx and not head,
    }


def untrack_index(paths: list[str]) -> tuple[list[str], list[str]]:
    """Remove *paths* from the real index (content stays on disk). Returns
    (changed, failed)."""
    changed: list[str] = []
    failed: list[str] = []
    for path in paths:
        cp = git("update-index", "--force-remove", "--", path)
        if cp.returncode == 0:
            changed.append(f"untracked-index:{path}")
        else:
            failed.append(f"{path}: {cp.stderr.strip()}")
    return changed, failed


def commit_untrack_on_head(paths: list[str]) -> tuple[list[str], list[str]]:
    """Commit removal of *paths* on top of HEAD via a temporary index.

    Never touches the real index or the working tree, so a worker's staged /
    unstaged state survives unchanged. Returns (changed, failed)."""
    changed: list[str] = []
    failed: list[str] = []
    with tempfile.NamedTemporaryFile(prefix="cron-store-untrack-idx-") as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": tmp.name}
        cp = git("read-tree", "HEAD", env=env)
        if cp.returncode != 0:
            return [], [f"read-tree HEAD: {cp.stderr.strip()}"]
        cp = git("update-index", "--force-remove", "--", *paths, env=env)
        if cp.returncode != 0:
            return [], [f"update-index (temp): {cp.stderr.strip()}"]
        cp = git("write-tree", env=env)
        if cp.returncode != 0:
            return [], [f"write-tree: {cp.stderr.strip()}"]
        tree = cp.stdout.strip()
        cp = git("commit-tree", tree, "-p", "HEAD", "-m", UNTRACK_COMMIT_MESSAGE)
        if cp.returncode != 0:
            return [], [f"commit-tree: {cp.stderr.strip()}"]
        new_sha = cp.stdout.strip()
        cp = git("update-ref", "-m", "cron-store-untrack-guard: drop tracked live stores",
                 "HEAD", new_sha)
        if cp.returncode != 0:
            return [], [f"update-ref HEAD {new_sha}: {cp.stderr.strip()}"]
        changed.append(f"untrack-commit:{new_sha}")
        changed.extend(f"untracked-head:{p}" for p in paths)
    return changed, failed


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Enforce that live cron stores are never tracked by git")
    ap.add_argument("--apply", action="store_true",
                    help="untrack any tracked store and commit the untracking on HEAD")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true",
                    help="with --json: suppress output when healthy and unchanged")
    args = ap.parse_args()

    try:
        state = audit()
    except RuntimeError as exc:
        print(f"cron-store-git-clobber-guard ERROR: {exc}", file=sys.stderr)
        return 2

    changed: list[str] = []
    failed: list[str] = []
    if args.apply and not state["healthy"]:
        if state["index_tracked"]:
            c, f = untrack_index(state["index_tracked"])
            changed += c
            failed += f
        if state["head_tracked"]:
            try:
                op = git_op_in_progress()
            except RuntimeError as exc:
                op = None
                failed.append(f"in-progress-check: {exc}")
            if op:
                failed.append(
                    f"head-tracked-but-{op}-in-progress: {len(state['head_tracked'])} "
                    "store(s) still in HEAD's tree; auto-commit skipped — re-run "
                    "--apply after the operation completes (the 2m canary will)")
            else:
                c, f = commit_untrack_on_head(state["head_tracked"])
                changed += c
                failed += f
        try:
            state = audit()
        except RuntimeError as exc:
            print(f"cron-store-git-clobber-guard ERROR: {exc}", file=sys.stderr)
            return 2

    if args.json:
        if args.quiet and state["healthy"] and not changed and not failed:
            return 0
        print(json.dumps({**state, "changed": changed, "failed": failed},
                         indent=2, sort_keys=True))
        return 2 if failed else 0

    if failed:
        print("cron-store-git-clobber-guard: FAILED to repair:")
        for f in failed:
            print(f"  {f}")
        return 2

    if changed:
        print(f"cron-store-git-clobber-guard: repaired git tracking of live cron "
              f"store(s) ({len(changed)} action(s)):")
        for p in changed:
            print(f"  {p}")
        return 0

    if state["healthy"]:
        # Silent when healthy — watchdog contract.
        return 0

    print("# live cron stores are TRACKED by git\n")
    print(f"{len(state['index_tracked'])} store(s) in the index, "
          f"{len(state['head_tracked'])} in HEAD's tree of {REPO}. Any checkout/"
          "reset/rebase will clobber live scheduler runtime state (next_run_at) "
          "with sanitized copies and silently defer every cron job "
          "(t_3c33bc49 / t_6c32b13c / 2026-08-05 outage class).\n")
    for p in state["index_tracked"]:
        print(f"  - index: {p}")
    for p in state["head_tracked"]:
        print(f"  - HEAD:  {p}")
    print(f"\nRemediate: {Path(__file__).name} --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
