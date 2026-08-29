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
import datetime
import hashlib
import os
import re
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


def has_unpushed(path: str) -> int:
    """Commits reachable from this tree's branches but from NO remote.

    2026-08-27 (Frank-authorised gate lift): the reaper's only cleanliness test
    was `git status --porcelain`, i.e. UNCOMMITTED tracked changes. A worktree
    holding COMMITTED-but-unpushed work passed as clean. Measured on the day the
    gate was lifted: 6 of 6 sampled candidates held 16 unpushed commits each,
    including the PR #1227 rebase the Session Bus recorded as a local immutable
    candidate. Removing a worktree orphans those commits and they are gone.
    A non-zero result here is an absolute veto.
    """
    try:
        out = subprocess.run(
            # HEAD, not --branches. Worktrees SHARE ONE OBJECT STORE, so
            # `--branches --not --remotes` asks "does this REPO hold unpushed
            # work?" and returns the same non-zero answer in all ~127 trees --
            # a veto that fires everywhere and reaps nothing. Scoping to HEAD
            # asks the question actually being asked: "does THIS tree's own
            # checkout hold commits no remote has?" (2026-08-27; same
            # shared-object-store trap that produced a bogus 2,204-orphan-commit
            # count earlier the same day).
            ["git", "log", "HEAD", "--not", "--remotes", "--oneline"],
            cwd=path, capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return -1          # cannot tell -> treat as unsafe
        return len([l for l in out.stdout.splitlines() if l.strip()])
    except Exception:
        return -1              # probe failed -> treat as unsafe


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


def head_sha(path: str) -> str | None:
    """Resolve this worktree's own HEAD. None means we cannot pin, so we must not remove."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def pin_head(sha: str, name: str, stamp: str, path: str = "") -> str | None:
    """Write refs/reaped/<stamp>/<name> in the PARENT repo, then VERIFY it resolves.

    `git worktree remove` orphans whatever the tree's HEAD pointed at, and once
    gc runs those objects are gone for good. A ref in the parent repo keeps them
    reachable and makes the removal undoable:
        git log refs/reaped/<stamp>/<name>
        git worktree add <path> refs/reaped/<stamp>/<name>
    The ref lives in the parent because the worktree that held it is about to
    stop existing.

    Returns the ref name only if it was written AND reads back as `sha`. A pin we
    have not confirmed is not a pin, and an unconfirmed pin must never authorise
    a delete.
    """
    # Disambiguate by full PATH, not basename. Two worktrees can share a basename
    # in different parents -- e.g. /home/frank/.worktrees/t_47de30ea and
    # /home/frank/sycode-trading/.worktrees/t_47de30ea were BOTH candidates on
    # 2026-08-28. Keying the ref on basename alone made the second update-ref
    # silently overwrite the first, so one batch reaped 25 trees and wrote 24
    # pins. update-ref does not keep a reflog in this namespace, so an overwritten
    # pin is simply gone. (No harm on that occasion: has_unpushed() had already
    # proved every reaped HEAD was on a remote. Belt-and-braces still has to work.)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "unnamed"
    disc = hashlib.sha1((path or name).encode("utf-8")).hexdigest()[:8]
    ref = f"refs/reaped/{stamp}/{safe}-{disc}"
    try:
        w = subprocess.run(["git", "-C", REPO, "update-ref", ref, sha],
                           capture_output=True, text=True, timeout=30)
        if w.returncode != 0:
            return None
        v = subprocess.run(["git", "-C", REPO, "rev-parse", ref],
                           capture_output=True, text=True, timeout=30)
        return ref if (v.returncode == 0 and v.stdout.strip() == sha) else None
    except Exception:
        return None


def remove_worktree(path: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["git", "-C", REPO, "worktree", "remove", "--force", path],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip()[:200]
        # rc=0 is not proof. Confirm the directory is actually gone before
        # reporting success, so a silent no-op cannot be counted as a reap.
        if os.path.exists(path):
            return False, "git reported success but the path still exists"
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


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
    skipped_unpushed = []
    for e in entries:
        p = e["path"]
        if is_protected(p):
            protected += 1
            continue
        if "/.worktrees/" not in p:  # managed by something else; don't touch
            managed += 1
            continue
        u = has_unpushed(p)
        if u != 0:
            reason = "PROBE-FAILED" if u < 0 else f"{u} UNPUSHED commits"
            skipped_unpushed.append((p, reason))
            continue
        if not is_clean(p):
            dirty += 1
            continue
        would_prune += 1
        candidates.append(p)

    if skipped_unpushed:
        print(f"PROTECTED ({len(skipped_unpushed)}) — unpushed work, NEVER removed:")
        for p, why in skipped_unpushed:
            print(f"  ! {p}  [{why}]")
    mode = os.environ.get("REAPER_MODE", "DRY-RUN").upper()
    print(f"MODE={mode}" + (" (read-only; no worktrees removed)" if mode != "REAP" else
                            " (REAPING — safe trees only)"))
    print(f"REPO={REPO}")
    print(
        f"Summary: {len(entries)} total, {would_prune} would-prune, "
        f"{dirty} dirty/skip, {managed} managed/skip, {protected} protected/skip"
    )
    if not candidates:
        return

    if mode != "REAP":
        print("Candidates (WOULD be pruned if reaping enabled):")
        for c in candidates:
            print(f"  - {c}")
        # __file__ is "<stdin>" here (the python is piped in), so name the shell script.
        print("\nTo actually reap: REAPER_MODE=REAP bash "
              "/home/frank/.hermes/profiles/jarvis/scripts/sycode_worktree_reaper.sh")
        return

    # ---- REAP: pin first, verify the pin, only then remove -------------------
    cap = int(os.environ.get("REAPER_MAX_REMOVE", "25"))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"\nREAPING (cap={cap}, pin namespace refs/reaped/{stamp}/)")
    removed = pinfail = rmfail = 0
    for path in candidates[:cap]:
        name = os.path.basename(path.rstrip("/"))
        sha = head_sha(path)
        if not sha:
            print(f"  SKIP {name}: cannot resolve HEAD, refusing to remove unpinnable tree")
            pinfail += 1
            continue
        ref = pin_head(sha, name, stamp, path)
        if not ref:
            print(f"  SKIP {name}: pin failed or did not verify — NOT removing")
            pinfail += 1
            continue
        ok, err = remove_worktree(path)
        if ok:
            removed += 1
            print(f"  REAPED {name}  pinned={ref} sha={sha[:9]}")
        else:
            rmfail += 1
            print(f"  FAILED {name}: {err} (pin {ref} kept)")
    skipped = max(0, len(candidates) - cap)
    print(f"\nREAPED {removed}, pin-refused {pinfail}, remove-failed {rmfail}"
          + (f", {skipped} left for the next run (cap)" if skipped else ""))
    if removed:
        print(f"UNDO any of them:  git -C {REPO} worktree add <path> refs/reaped/{stamp}/<name>")
        print(f"LIST the pins:     git -C {REPO} for-each-ref refs/reaped/{stamp}/")


if __name__ == "__main__":
    main()
PY
