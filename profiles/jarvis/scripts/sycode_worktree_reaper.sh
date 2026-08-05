#!/usr/bin/env bash
# CANONICAL SOURCE — created for kanban task t_e93b3fba (Beat 4 first-run preflight fix).
#
# sycode-worktree-reaper — READ-ONLY DRY-RUN.
#
# Cron job eff5a150e679 (profile: jarvis, schedule "0 5 * * 0") referenced
# sycode_worktree_reaper.sh, but that file did not exist at the canonical
# scripts path, so the job would fail its first run. This script makes the
# job runnable and SAFE.
#
#   MODE = DRY-RUN. No worktrees are removed. It only reports which worktrees
#   WOULD be pruned if reaping were enabled.
#
# Reaping (git worktree remove --force) requires os-reviewer sign-off, per the
# task gate ("Read-only script only"). See the follow-up review card.
#
# Behaviour mirrors the proven sycode_worktree_reaper.py (generalized from the
# sycode-trading production reaper that pruned 232 worktrees in one pass):
#   - Exact-path protection for the primary checkout.
#   - Prefix protection for Hermes/kanban/tmp managed trees.
#   - Only worktrees nested under <repo>/.worktrees/ are considered.
#   - "Clean" = no uncommitted TRACKED changes (untracked ?? ignored).
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/home/frank/sycode-trading}"

python3 - "$REPO_ROOT" <<'PY'
import os
import subprocess
import sys

REPO = os.path.realpath(sys.argv[1])

# Exact-match protection: the primary checkout itself.
PROTECTED_PATH_EXACT = [REPO]
# Prefix protection: Hermes/kanban/tmp managed trees (outside repo tree).
PROTECTED_PREFIXES = [
    "/home/frank/.hermes/deploy-state/build-tree",
    "/home/frank/.hermes/kanban/boards",
    "/home/frank/.hermes/worktrees",
    "/home/frank/.local/share/hermes",
    "/tmp/claude",
    "/tmp/hermes",
]


def is_protected(path: str) -> bool:
    resolved = os.path.realpath(path)
    if resolved in PROTECTED_PATH_EXACT:
        return True
    return any(resolved.startswith(p) for p in PROTECTED_PREFIXES)


def is_clean(path: str) -> bool:
    """True if no uncommitted TRACKED changes. Untracked (??) ignored."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return False
        for line in r.stdout.strip().splitlines():
            if not line.startswith("??"):
                return False  # a tracked file is modified/deleted/renamed
        return True
    except Exception:
        return False


def main():
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
    except Exception as ex:
        print(f"ERROR: cannot list worktrees for {REPO}: {ex}", file=sys.stderr)
        sys.exit(1)

    entries, cur = [], {}
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            if cur:
                entries.append(cur)
            cur = {"path": line[9:].strip()}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].strip()
        elif line == "":
            if cur:
                entries.append(cur)
            cur = {}
    if cur:
        entries.append(cur)

    would_prune = dirty = managed = protected = 0
    candidates = []
    for e in entries:
        p = e["path"]
        if is_protected(p):
            protected += 1
            continue
        if "/.worktrees/" not in p:  # managed by something else; don't touch
            managed += 1
            continue
        if not is_clean(p):
            dirty += 1
            continue
        would_prune += 1
        candidates.append(p)

    print("MODE=DRY-RUN (read-only; no worktrees removed)")
    print(f"REPO={REPO}")
    print(
        f"Summary: {len(entries)} total, {would_prune} would-prune, "
        f"{dirty} dirty/skip, {managed} managed/skip, {protected} protected/skip"
    )
    if candidates:
        print("Candidates (WOULD be pruned if reaping enabled):")
        for c in candidates:
            print(f"  - {c}")


if __name__ == "__main__":
    main()
