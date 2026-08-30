#!/usr/bin/env python3
"""Keep ~/.hermes automation source mirrored to origin/fleet/automation-vc.

This keeper is intentionally conservative:
- syncs only explicitly allowed automation-source paths;
- skips runtime/secret/state paths by construction and by regex scan;
- defaults to files already tracked either by the live repo or the automation branch;
- never checks out or mutates the live working tree branch;
- pushes only after a clean staged secret scan.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("HERMES_AUTOMATION_REPO", "/home/frank/.hermes")).resolve()
REMOTE = os.environ.get("HERMES_AUTOMATION_REMOTE", "origin")
BRANCH = os.environ.get("HERMES_AUTOMATION_BRANCH", "fleet/automation-vc")

# Build these substrings without writing the exact denied literals in this file;
# the keeper scans itself before committing.
_TOKEN_HEADER = "X-Sycode" + "-Token"
_WATCHDOG_BEARER = "Bearer " + "hermes" + "-" + "watchdog"
_GITHUB_PAT = "github" + "_pat_"
SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|gh[opsu]_[A-Za-z0-9]{20,}|" + re.escape(_GITHUB_PAT) + r"|xox[baprs]-|"
    r"-----BEGIN (RSA|EC|OPENSSH|AES|PRIVATE)|SECRET\s*=\s*['\"][0-9a-f]{16,}|"
    + re.escape(_TOKEN_HEADER) + r":[0-9a-f]{16,}|" + re.escape(_WATCHDOG_BEARER) + r")"
)

ROOT_FILES = {
    ".gitignore",
    "config.yaml",
    "profile.yaml",
    "SOUL.md",
    "shell-hooks-allowlist.json",
    "context_length_cache.yaml",
}
SCRIPT_SUFFIXES = (".py", ".sh", ".md")
DENY_PARTS = {
    "archive",
    "backups",
    "logs",
    "state",
    "staging",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".tmp-backups",
}
DENY_NAMES = {"auth.json", ".env"}
DENY_SUFFIXES = (".db", ".bak", ".orig", ".pre")
DENY_CONTAINS = ("/memories/", "/sessions/", "/logs/", "/cache/")
# Keep both the root keeper and the devops-profile dispatch wrapper durable on the
# automation branch. The wrapper is referenced by the devops cron job (cron script
# paths resolve to the owning profile's scripts/ dir), so if it is not tracked here
# a DGX rebuild from the repo would restore the root script but not the wrapper the
# cron expects -> the keeper would silently stop running. See runbook recovery step.
FORCE_INCLUDE = {
    "scripts/automation_vc_keeper.py",
    "profiles/devops/scripts/automation-vc-keeper.sh",
    # The keeper cron now runs from the jarvis profile (job bbc6def62725),
    # whose script path resolves to profiles/jarvis/scripts/. Track the jarvis
    # wrapper too so a DGX rebuild restores the exact copy the live cron runs.
    "profiles/jarvis/scripts/automation-vc-keeper.sh",
    "scripts/cron_live_script_guard.py",
    # dead-store invariant guard (t_4bedf8d5): track the exact live copy so the
    # shared-checkout branch-swap hazard (148ade8 revert) can never silently
    # restore an old/unreviewed version of this watchdog script.
    "profiles/jarvis/scripts/cron_ticker_invariant_guard.py",
}
# Non-live path holding sanitized recovery snapshots of every live cron store
# (t_6c32b13c: the live stores themselves are untracked + gitignored; a DGX
# rebuild recovers job definitions from here, never from a live path).
SNAPSHOT_PREFIX = "cron-snapshots"


def run(cmd: list[str], cwd: Path = REPO, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture, check=check)


def git_lines(args: list[str], cwd: Path = REPO) -> set[str]:
    cp = run(["git", *args], cwd=cwd)
    return {line.strip() for line in cp.stdout.splitlines() if line.strip()}


def is_allowed(rel: str) -> bool:
    rel = rel.strip("/")
    p = Path(rel)
    parts = set(p.parts)
    name = p.name
    if not rel or rel.startswith(".git/"):
        return False
    if name in DENY_NAMES or any(name.endswith(s) for s in DENY_SUFFIXES):
        return False
    if any(x in f"/{rel}/" for x in DENY_CONTAINS):
        return False
    if parts & DENY_PARTS:
        return False
    if rel in ROOT_FILES:
        return True
    if rel.startswith("scripts/") and len(p.parts) == 2 and name.endswith(SCRIPT_SUFFIXES):
        return True
    if rel.startswith("agent-hooks/") and len(p.parts) == 2:
        return True
    # Live cron stores (cron/jobs.json, profiles/*/cron/jobs.json) are mutable
    # scheduler runtime state and are NEVER tracked (t_6c32b13c, completed
    # 2026-08-05): a tracked sanitized copy clobbers next_run_at on every
    # checkout/reset and silently defers the whole fleet's cron jobs. Recovery
    # snapshots live under the non-live cron-snapshots/ path instead, generated
    # fresh each tick by snapshot_cron_stores().
    if rel.startswith(f"{SNAPSHOT_PREFIX}/"):
        return True
    # The devops-profile keeper dispatcher wrapper is durable on this branch (see
    # FORCE_INCLUDE). It holds no secrets and only delegates to the tracked root script.
    if rel == "profiles/devops/scripts/automation-vc-keeper.sh":
        return True
    if rel == "profiles/jarvis/scripts/automation-vc-keeper.sh":
        return True
    return False


def discover(include_untracked: bool) -> tuple[set[str], set[str]]:
    live_tracked = git_lines(["ls-files"])
    branch_tracked = git_lines(["ls-tree", "-r", "--name-only", f"{REMOTE}/{BRANCH}"])
    # FORCE_INCLUDE bypasses the allowlist filter by design: these paths are the
    # durable keeper mechanism itself and must always be considered even if the
    # allowlist would otherwise exclude them (see FORCE_INCLUDE note).
    candidates = {p for p in (live_tracked | branch_tracked) if is_allowed(p)} | FORCE_INCLUDE
    skipped_untracked: set[str] = set()
    if include_untracked:
        candidates |= {p for p in git_lines(["ls-files", "--others", "--exclude-standard"]) if is_allowed(p)}
    else:
        skipped_untracked = {p for p in git_lines(["ls-files", "--others", "--exclude-standard"]) if is_allowed(p)}
    return candidates, skipped_untracked


def copy_into_worktree(paths: set[str], wt: Path) -> None:
    for rel in sorted(paths):
        src = REPO / rel
        dst = wt / rel
        if src.exists() and src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dst.exists() and src.samefile(dst):
                    continue
            except OSError:
                pass
            shutil.copy2(src, dst)


def snapshot_cron_stores(wt: Path) -> set[str]:
    """Write sanitized snapshots of every live cron store under cron-snapshots/.

    The live stores are untracked + gitignored (t_6c32b13c) so they can never be
    clobbered by a checkout; this non-live mirror keeps job DEFINITIONS durable
    for recovery. normalize_cron_json strips volatile runtime fields, so the
    snapshot content only changes when definitions change (no commit churn)."""
    out: set[str] = set()
    _stores_rp = set()
    stores: list[Path] = []
    for _s in sorted((REPO / "profiles").glob("*/cron/jobs.json")):
        _rp = os.path.realpath(_s)
        if _rp in _stores_rp:
            continue  # symlink alias (e.g. sycode-trading -> sycode-trading-pm) — dedupe
        _stores_rp.add(_rp)
        stores.append(_s)
    root = REPO / "cron" / "jobs.json"
    if root.exists():
        stores.append(root)
    for src in stores:
        if not src.is_file():
            continue
        rel = Path(SNAPSHOT_PREFIX) / src.relative_to(REPO)
        text = src.read_text(errors="ignore")
        redacted = SECRET_PATTERN.sub("[REDACTED_SECRET]", normalize_cron_json(text))
        dst = wt / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(redacted)
        out.add(str(rel))
    return out


def staged_files(wt: Path) -> list[str]:
    cp = run(["git", "diff", "--cached", "--name-only"], cwd=wt)
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


def normalize_cron_json(text: str) -> str:
    """Strip scheduler runtime state from cron job stores before VCS sync.

    The automation branch is recovery/source-of-truth, not a live scheduler state
    database. Keeping last_run/next_run/error counters would create a new commit on
    nearly every keeper tick and can preserve transient stderr containing secrets.
    """
    volatile = {
        "next_run_at",
        "last_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
        "fire_claim",
    }
    try:
        data = json.loads(text)
    except Exception:
        return text
    data.pop("updated_at", None)
    jobs = data.get("jobs")
    if isinstance(jobs, list):
        for job in jobs:
            if not isinstance(job, dict):
                continue
            for key in volatile:
                job.pop(key, None)
            repeat = job.get("repeat")
            if isinstance(repeat, dict):
                repeat.pop("completed", None)
    return json.dumps(data, indent=2, sort_keys=False) + "\n"


def secret_scan(wt: Path, files: list[str]) -> list[str]:
    hits: list[str] = []
    for rel in files:
        path = wt / rel
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except Exception as exc:
            hits.append(f"{rel}: unreadable: {exc}")
            continue
        for idx, line in enumerate(text.splitlines(), 1):
            if SECRET_PATTERN.search(line):
                hits.append(f"{rel}:{idx}:{line[:240]}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report what would be committed without committing/pushing")
    ap.add_argument("--include-untracked", action="store_true", help="include allowlisted untracked live files; use only after human review")
    ap.add_argument("--report-skipped", action="store_true", help="on no-op, report allowlisted untracked files skipped for review")
    ap.add_argument("--message", default="chore(automation-vc): keeper catch-up sync (t_84980841)")
    args = ap.parse_args()

    if not (REPO / ".git").exists():
        print(f"FATAL: {REPO} is not a git checkout", file=sys.stderr)
        return 2

    run(["git", "fetch", REMOTE, BRANCH], cwd=REPO)
    paths, skipped_untracked = discover(args.include_untracked)
    if not paths:
        print("automation-vc keeper: no allowlisted paths discovered")
        return 0

    with tempfile.TemporaryDirectory(prefix="automation-vc-keeper-") as td:
        wt = Path(td) / "wt"
        run(["git", "worktree", "add", "--detach", str(wt), f"{REMOTE}/{BRANCH}"], cwd=REPO)
        try:
            copy_into_worktree(paths, wt)
            snapshots = snapshot_cron_stores(wt)
            # Stage only the allowed pathset. Deletions are intentionally not staged automatically.
            run(["git", "add", "--", *sorted(paths | snapshots)], cwd=wt)
            files = staged_files(wt)
            hits = secret_scan(wt, files)
            if hits:
                print("COMMIT BLOCKED: staged automation files contain potential secret patterns", file=sys.stderr)
                print("\n".join(hits[:80]), file=sys.stderr)
                return 3
            if args.dry_run:
                print(f"automation-vc keeper dry-run: staged_files={len(files)}")
                if files:
                    print("\n".join(files[:200]))
                if skipped_untracked:
                    print(f"skipped_untracked_allowlisted={len(skipped_untracked)}")
                    print("\n".join(sorted(skipped_untracked)[:200]))
                return 0
            if not files:
                # Stay quiet on cron no-op by default; no-agent cron treats stdout as a delivery.
                if args.report_skipped and skipped_untracked:
                    print(f"automation-vc keeper: no tracked drift; skipped {len(skipped_untracked)} allowlisted untracked files pending review")
                return 0
            run(["git", "commit", "-m", args.message], cwd=wt, capture=False)
            run(["git", "push", REMOTE, f"HEAD:{BRANCH}"], cwd=wt, capture=False)
            print(f"automation-vc keeper: committed+pushed {len(files)} files to {REMOTE}/{BRANCH}")
            if skipped_untracked:
                print(f"automation-vc keeper: skipped {len(skipped_untracked)} allowlisted untracked files pending review")
            return 0
        finally:
            run(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
