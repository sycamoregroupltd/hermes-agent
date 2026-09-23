#!/usr/bin/env python3
"""Keep ~/.hermes automation source mirrored to origin/fleet/automation-vc.

This keeper is intentionally conservative:
- syncs only explicitly allowed automation-source paths;
- skips runtime/secret/state paths by construction and by regex scan;
- defaults to files already tracked either by the live repo or the automation branch;
- never checks out or mutates the live working tree branch;
- pushes only after a clean staged secret scan;
- NEVER pushes directly to fleet/automation-vc; opens or updates draft PRs instead.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

REPO = Path(os.environ.get("HERMES_AUTOMATION_REPO", "/home/frank/.hermes")).resolve()
REMOTE_URL = os.environ.get(
    "HERMES_AUTOMATION_REMOTE_URL",
    "git@github.com:sycamoregroupltd/hermes-dgx-fleet-automation.git",
)
BRANCH = os.environ.get("HERMES_AUTOMATION_BRANCH", "fleet/automation-vc")


def github_repo_from_remote(remote_url: str) -> str:
    """Map git remote URL to owner/name for `gh -R` (live ~/.hermes origin is hermes-agent)."""
    expected = "sycamoregroupltd/hermes-dgx-fleet-automation"
    if expected not in remote_url:
        raise ValueError(f"Refuse gh -R: remote {remote_url!r} is not {expected}")
    return expected



def validate_remote_url(value: str) -> str:
    """Reject credential-bearing transport overrides before invoking git.

    The normal scp-like ``git@github.com:owner/repo.git`` form is allowed: its
    ``git`` prefix is the transport account, not an embedded password/token.
    URL-form transports must not include either a username or password because
    the override is routinely surfaced in diagnostics and cron configuration.
    """
    if not value or value != value.strip():
        raise ValueError("HERMES_AUTOMATION_REMOTE_URL must be a non-empty URL without surrounding whitespace")
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ValueError("HERMES_AUTOMATION_REMOTE_URL is not a valid transport URL") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("HERMES_AUTOMATION_REMOTE_URL must not contain embedded credentials")
    return value

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
# Keep the root keeper and both profile dispatch wrappers durable on the automation
# branch. The current Jarvis cron row resolves its script path against the Jarvis
# profile, while the Devops wrapper is retained for older deployments. If the
# current wrapper is not tracked here, a DGX rebuild would restore the root script
# but not the wrapper the scheduler expects -> the keeper would silently stop.
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


def redact(text: str) -> str:
    """Keep command failures actionable without persisting credentials."""
    text = SECRET_PATTERN.sub("[REDACTED_SECRET]", text)
    return re.sub(r"(https?://)[^/@\s]+@", r"\1[REDACTED_CREDENTIAL]@", text)


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], error: subprocess.CalledProcessError):
        detail = "\n".join(part for part in (error.stderr, error.stdout) if part)
        message = f"command failed (exit {error.returncode}): {redact(shlex.join(cmd))}"
        if detail:
            message += f"\n{redact(detail[-4000:])}"
        super().__init__(message)
        self.returncode = error.returncode


def run(cmd: list[str], cwd: Path = REPO, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command while retaining failure diagnostics even when output is hidden.

    ``capture=False`` suppresses successful output from the returned process, but
    output is still captured internally so a failing commit/push cannot lose its
    stderr before ``CommandError`` redacts and reports it.
    """
    try:
        result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)
    except subprocess.CalledProcessError as error:
        raise CommandError(cmd, error) from error
    if not capture:
        return cast(
            subprocess.CompletedProcess[str],
            subprocess.CompletedProcess(result.args, result.returncode, None, None),
        )
    return result


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
    if rel.startswith("scripts/tests/") and len(p.parts) == 3 and name.endswith(".py"):
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
    # Profile-local cron scripts (t_4b7afeac carve-out; gap closed by t_881f0f42).
    # A cron `script` value resolves to the OWNING profile's scripts/ dir (there is
    # no global scripts/ fallback), so profiles/<p>/scripts/<file> IS the automation
    # source that actually runs — including every guard-bundle manifest check. The
    # .gitignore carve-out already re-includes exactly this shape
    # (depth-1 *.py|*.sh, scripts/tests/*.py, the frank-gate-probe subtree), but
    # until 2026-09-23 is_allowed() permitted only the two keeper wrappers below, so
    # the keeper carried no later edits to any other profile script — the
    # "Known allowlist gap" in AUTOMATION-VC-RECOVERY-RUNBOOK.md.
    #
    # Scope note: SCRIPT_SUFFIXES deliberately not reused here — the .gitignore rule
    # re-includes only .py/.sh under a profile scripts dir, never .md.
    if (
        len(p.parts) == 4
        and p.parts[0] == "profiles"
        and p.parts[2] == "scripts"
        and name.endswith((".py", ".sh"))
    ):
        return True
    if (
        len(p.parts) == 5
        and p.parts[0] == "profiles"
        and p.parts[2] == "scripts"
        and p.parts[3] in ("tests", "frank-gate-probe")
        and name.endswith(".py")
    ):
        return True
    if (
        len(p.parts) == 5
        and p.parts[0] == "profiles"
        and p.parts[2] == "scripts"
        and p.parts[3] == "frank-gate-probe"
        and name.endswith(".sh")
    ):
        return True
    # The keeper dispatcher wrappers are durable on this branch (see FORCE_INCLUDE).
    # They hold no secrets and only delegate to the tracked root script.
    if rel == "profiles/devops/scripts/automation-vc-keeper.sh":
        return True
    if rel == "profiles/jarvis/scripts/automation-vc-keeper.sh":
        return True
    return False


def discover(include_untracked: bool, branch_ref: str) -> tuple[set[str], set[str]]:
    live_tracked = git_lines(["ls-files"])
    branch_tracked = git_lines(["ls-tree", "-r", "--name-only", branch_ref])
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


def generate_keeper_branch_name() -> str:
    """Generate a deterministic keeper branch name based on current timestamp."""
    now = datetime.datetime.now(datetime.timezone.utc)
    return f"keeper/sync-{now.strftime('%Y%m%d-%H%M%S')}"


def find_existing_keeper_pr(base_branch: str, remote_url: str) -> str | None:
    """Find an existing open draft PR created by the keeper.
    
    Returns the PR number as a string if found, None otherwise.
    Fails closed: returns None on any gh command failure.
    """
    try:
        result = run(
            ["gh", "pr", "list", "-R", github_repo_from_remote(remote_url), "--base", base_branch, "--state", "open", "--json", "number,headRefName,isDraft"],
            cwd=REPO,
            check=False,
        )
        if result.returncode != 0:
            return None
        prs = json.loads(result.stdout)
        for pr in prs:
            if pr.get("isDraft") and pr.get("headRefName", "").startswith("keeper/sync-"):
                return str(pr["number"])
    except Exception:
        pass
    return None


def validate_keeper_branch_safe(branch_name: str, base_branch: str, remote_url: str) -> None:
    """Validate that a branch is safe to push to (keeper branch, not trunk, correct repo).
    
    Raises ValueError if the branch is unsafe. Checks:
    - Branch is not the base branch or any trunk branch
    - Branch starts with 'keeper/sync-'
    - Remote URL matches the expected repository
    
    Fails closed: rejects any branch that could be trunk or wrong repo.
    Check order matters for error message clarity.
    """
    if branch_name == base_branch:
        raise ValueError(f"Unsafe branch: '{branch_name}' matches base branch '{base_branch}'")
    
    unsafe_refs = {"main", "master", "fleet/automation-vc", "develop", "production"}
    if branch_name in unsafe_refs:
        raise ValueError(f"Unsafe branch: '{branch_name}' is a protected trunk branch")
    
    if not branch_name or not branch_name.startswith("keeper/sync-"):
        raise ValueError(f"Unsafe branch: '{branch_name}' does not start with 'keeper/sync-'")
    
    expected_repo = "sycamoregroupltd/hermes-dgx-fleet-automation"
    if expected_repo not in remote_url:
        raise ValueError(f"Unsafe remote: '{remote_url}' does not contain expected repo '{expected_repo}'")


def create_or_update_pr(wt: Path, keeper_branch: str, base_branch: str, commit_message: str, remote_url: str) -> None:
    """Create a new draft PR or update an existing keeper PR.
    
    Fails closed: if gh commands fail, raises CommandError; no fallback to direct push.
    All pushes use the validated remote URL and validate branch safety immediately before push.
    """
    existing_pr = find_existing_keeper_pr(base_branch, remote_url)
    
    if existing_pr:
        result = run(
            ["gh", "pr", "view", existing_pr, "-R", github_repo_from_remote(remote_url), "--json", "headRefName,baseRefName,headRefOid"],
            cwd=REPO,
            check=False,
        )
        if result.returncode == 0:
            pr_data = json.loads(result.stdout)
            existing_branch = pr_data.get("headRefName")
            pr_base = pr_data.get("baseRefName")
            head_oid = pr_data.get("headRefOid")
            
            if pr_base != base_branch:
                raise ValueError(
                    f"Existing PR #{existing_pr} base changed from expected '{base_branch}' to '{pr_base}'; refusing fall-through"
                )
            
            if existing_branch and head_oid:
                validate_keeper_branch_safe(existing_branch, base_branch, remote_url)
                
                run(
                    ["git", "push", f"--force-with-lease=refs/heads/{existing_branch}:{head_oid}", f"{remote_url}", f"HEAD:refs/heads/{existing_branch}"],
                    cwd=wt,
                    capture=False,
                )
                print(f"automation-vc keeper: updated existing draft PR #{existing_pr} on branch {existing_branch}")
                return
            elif existing_branch and not head_oid:
                raise ValueError(f"Existing PR #{existing_pr} missing headRefOid; cannot verify lease for safe push")
            elif not existing_branch:
                raise ValueError(f"Existing PR #{existing_pr} missing headRefName; cannot update safely")
    
    validate_keeper_branch_safe(keeper_branch, base_branch, remote_url)
    run(["git", "push", "-u", remote_url, f"HEAD:refs/heads/{keeper_branch}"], cwd=wt, capture=False)
    
    pr_body = f"""Automated keeper sync: {commit_message}

This draft PR contains changes detected by the automation-vc keeper.

**Review before merging:**
- Verify no secrets or credentials are present
- Confirm all changes are intentional
- Check that sanitized cron snapshots are correct

This PR was automatically created by the keeper and should be reviewed before being marked ready and merged."""
    
    try:
        result = run(
            [
                "gh", "pr", "create",
                "-R", github_repo_from_remote(remote_url),
                "--draft",
                "--base", base_branch,
                "--head", keeper_branch,
                "--title", f"chore(automation-vc): keeper sync {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                "--body", pr_body,
            ],
            cwd=wt,
            capture=False,
        )
        print(f"automation-vc keeper: created draft PR for branch {keeper_branch}")
    except CommandError as e:
        print(f"automation-vc keeper: failed to create draft PR; branch {keeper_branch} pushed but no PR created", file=sys.stderr)
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report what would be committed without committing/pushing")
    ap.add_argument("--include-untracked", action="store_true", help="include allowlisted untracked live files; use only after human review")
    ap.add_argument("--report-skipped", action="store_true", help="on no-op, report allowlisted untracked files skipped for review")
    ap.add_argument("--message", default="chore(automation-vc): keeper catch-up sync (t_84980841)")
    args = ap.parse_args()

    try:
        remote_url = validate_remote_url(REMOTE_URL)
    except ValueError as error:
        print(f"automation-vc keeper ERROR: {error}", file=sys.stderr)
        return 2

    if not (REPO / ".git").exists():
        print(f"FATAL: {REPO} is not a git checkout", file=sys.stderr)
        return 2

    run(["git", "fetch", remote_url, BRANCH], cwd=REPO)
    branch_ref = run(["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"], cwd=REPO).stdout.strip()
    paths, skipped_untracked = discover(args.include_untracked, branch_ref)
    if not paths:
        print("automation-vc keeper: no allowlisted paths discovered")
        return 0

    with tempfile.TemporaryDirectory(prefix="automation-vc-keeper-") as td:
        wt = Path(td) / "wt"
        run(["git", "worktree", "add", "--detach", str(wt), branch_ref], cwd=REPO)
        try:
            copy_into_worktree(paths, wt)
            snapshots = snapshot_cron_stores(wt)
            # Stage only the allowed pathset. Deletions are intentionally not staged
            # automatically. A candidate that is tracked in the live repo but deleted
            # from its working tree (`git ls-files` still lists it) and absent from the
            # automation branch has NO blob in the worktree; `git add -- <path>` then
            # dies with "pathspec did not match", which aborted the entire tick before
            # anything was committed (t_881f0f42 — 86 such paths, 66 under scripts/).
            # Stage only paths that exist in the worktree and name the rest on stderr,
            # so the drift is visible without the keeper failing closed on it.
            to_stage = sorted(p for p in (paths | snapshots) if (wt / p).exists())
            absent = sorted(p for p in (paths | snapshots) if not (wt / p).exists())
            if absent:
                print(
                    f"automation-vc keeper: {len(absent)} allowlisted path(s) absent from the "
                    "worktree (tracked live, deleted on disk, not on the branch) — not staged",
                    file=sys.stderr,
                )
                print("\n".join(absent[:50]), file=sys.stderr)
            if to_stage:
                run(["git", "add", "--", *to_stage], cwd=wt)
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
            
            keeper_branch = generate_keeper_branch_name()
            try:
                create_or_update_pr(wt, keeper_branch, BRANCH, args.message, remote_url)
                print(f"automation-vc keeper: committed {len(files)} files and created/updated draft PR")
            except CommandError as e:
                print(f"automation-vc keeper ERROR: PR creation failed; no changes pushed to {BRANCH}", file=sys.stderr)
                raise
            
            if skipped_untracked:
                print(f"automation-vc keeper: skipped {len(skipped_untracked)} allowlisted untracked files pending review")
            return 0
        finally:
            run(["git", "worktree", "remove", "--force", str(wt)], cwd=REPO, check=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommandError as error:
        print(f"automation-vc keeper ERROR: {error}", file=sys.stderr)
        raise SystemExit(error.returncode)
