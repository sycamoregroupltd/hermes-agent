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
import fnmatch
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
    "cron/jobs.json",
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
DENY_SUFFIXES = (".db", ".bak")
DENY_CONTAINS = ("/memories/", "/sessions/", "/logs/", "/cache/")
FORCE_INCLUDE = {"scripts/automation_vc_keeper.py"}


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
    if fnmatch.fnmatch(rel, "profiles/*/cron/jobs.json"):
        return True
    return False


def discover(include_untracked: bool) -> tuple[set[str], set[str]]:
    live_tracked = git_lines(["ls-files"])
    branch_tracked = git_lines(["ls-tree", "-r", "--name-only", f"{REMOTE}/{BRANCH}"])
    candidates = {p for p in (live_tracked | branch_tracked | FORCE_INCLUDE) if is_allowed(p)}
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
            if rel == "cron/jobs.json" or fnmatch.fnmatch(rel, "profiles/*/cron/jobs.json"):
                text = src.read_text(errors="ignore")
                redacted = SECRET_PATTERN.sub("[REDACTED_SECRET]", text)
                dst.write_text(redacted)
                shutil.copystat(src, dst)
            else:
                shutil.copy2(src, dst)


def staged_files(wt: Path) -> list[str]:
    cp = run(["git", "diff", "--cached", "--name-only"], cwd=wt)
    return [line.strip() for line in cp.stdout.splitlines() if line.strip()]


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
            # Stage only the allowed pathset. Deletions are intentionally not staged automatically.
            run(["git", "add", "--", *sorted(paths)], cwd=wt)
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
