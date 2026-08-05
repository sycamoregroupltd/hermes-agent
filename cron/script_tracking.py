"""script_tracking.py — canonical git-tracking validation for cron job scripts.

This module provides deterministic author-time checks that prevent the
"untracked / unmerged cron script" failure mode described in t_89e30994.

It is designed to be imported by both:
- The hermes-agent product code (cronjob_tools → create_job/update_job)
- The fleet-side detector (/home/frank/.hermes/scripts/cron_untracked_script_guard.py)

Both must agree on resolution semantics, containment rules, and tracked-check
primitives. Divergence would itself be a bug.

Resolution model (mirrors scheduler.py:2328-2346):
  - Relative script  -> <profile_home>/scripts/<script>
  - Absolute script   -> resolved; must be contained within <profile_home>/scripts/
  - A script that resolves OUTSIDE the owning profile scripts/ dir is
    SCHEDULER-BLOCKED (scheduler will refuse it).
  - Only paths *inside* the Hermes repo are subject to git-tracking checks.
    Paths outside the repo are always accepted — the repo has no authority over them.

Tracked check: `git ls-files --error-unmatch <rel_path>` inside the repo.
This sees gitignored paths (unlike `--others` intersection), which is correct.

Exit codes for check_all():
  0 = all scripts tracked (or out-of-repo — not this repo's asset)
  1 = one or more violations
  2 = operational error (git not available, parse failure)

Fail-closed vs fail-loud strategy (documented for consumer integration):
  - For no_agent jobs: REFUSE outright (the script IS the job).
  - For agent jobs: WARN prominently (agent can recover if someone fixes
    it later), but return violations list for caller decision.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ScriptViolation:
    """Represents a single script-tracking violation."""
    job_id: str
    job_name: str
    profile: str
    script: str
    store: str
    reason: str  # short human-readable description
    via: str = ""  # "script-field", "command-ref:prompt", etc.


def git_repo_root(start: Path) -> Optional[Path]:
    """Resolve enclosing git repo root from *start*. Returns None if not in a git tree."""
    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).expanduser()
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None


def run_git(repo: Path, args: list[str]) -> tuple[int, str]:
    """Run a git command inside *repo*. Returns (rc, stdout)."""
    cmd = ["git", "-C", str(repo), *args]
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
    return r.returncode, r.stdout


def is_tracked(rel_path: str, repo: Path) -> bool:
    """True iff rel_path is tracked (committed or staged) in the given repo."""
    rc, _out = run_git(repo, ["ls-files", "--error-unmatch", "--", rel_path])
    return rc == 0


def resolve_script_path(profile_home: Path, script: str) -> tuple[Optional[Path], Optional[str]]:
    """Reproduce scheduler.py:2328-2346 resolution.

    Returns (resolved_path, error_reason_or_None).
    If the path escapes the owning profile scripts/ dir, returns (None, "SCHEDULER-BLOCKED").
    If the script does not exist on disk, returns (None, "MISSING").
    Otherwise returns (resolved_path, None).
    """
    scripts_dir = (profile_home / "scripts")
    scripts_dir_resolved = scripts_dir.resolve()
    raw = Path(script).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()

    # Containment check identical to scheduler.
    try:
        path.relative_to(scripts_dir_resolved)
    except ValueError:
        return None, "SCHEDULER-BLOCKED"

    if not path.exists():
        return None, "MISSING"

    return path, None


def validate_script_against_git(
    resolved_path: Path, repo: Path, *, include_outside_repo: bool = False
) -> Optional[str]:
    """Validate that a resolved script path is tracked in git.

    If the resolved path is outside the repo, returns None (not this repo's asset).
    If inside the repo and untracked, returns the relative path string.
    If tracked, returns None.
    """
    try:
        rel = str(resolved_path.relative_to(repo))
    except ValueError:
        if not include_outside_repo:
            return None  # outside repo: silently OK
        return "OUTSIDE-REPO"  # informational only
    if is_tracked(rel, repo):
        return None
    return rel  # untracked


def check_jobs_store(
    store_path: Path,
    repo: Path,
    profile_label: str,
    repo_relative_hermes_home: Optional[Path] = None,
) -> list[ScriptViolation]:
    """Check all enabled jobs in a single cron store for tracking violations.

    Args:
        store_path: Path to the jobs.json file.
        repo: The repository root containing the Hermes home.
        profile_label: Human-readable profile label (e.g., "jarvis", "<root>").
        repo_relative_hermes_home: If set, the store's HERMES_HOME relative to repo.
                                   Used when the store lives directly under REPO/profiles/X.
                                   When None, defaults to REPO/profiles/<label> or REPO.

    Returns:
        List of ScriptViolation objects.
    """
    import json
    try:
        data = json.loads(store_path.read_text())
    except Exception as exc:
        print(f"WARNING: could not parse store {store_path}: {exc}", file=sys.stderr)
        return []

    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        return []

    violations: list[ScriptViolation] = []

    # Determine profile home directory.
    if profile_label == "<root>":
        profile_home = repo
    elif repo_relative_hermes_home is not None:
        profile_home = repo_relative_hermes_home
    else:
        profile_home = repo / "profiles" / profile_label

    commands_fields = ("command", "prompt")
    script_token_re = None

    for job in jobs:
        # Skip disabled/paused jobs.
        if job.get("enabled") is False:
            continue
        if job.get("state") == "paused":
            continue
        if job.get("disabled") is True:
            continue

        job_id = job.get("id", "unknown")
        job_name = job.get("name", job_id)

        # Check script field.
        script = job.get("script")
        if script and isinstance(script, str):
            script = script.strip()
            if not script:
                continue
            resolved, reason = resolve_script_path(profile_home, script)
            if reason == "SCHEDULER-BLOCKED":
                violations.append(ScriptViolation(
                    job_id=job_id, job_name=job_name, profile=profile_label,
                    script=script, store=str(store_path),
                    reason=f"SCHEDULER-BLOCKED: script resolves outside profile scripts dir ({profile_home / 'scripts'})",
                    via="script-field",
                ))
            elif reason == "MISSING":
                violations.append(ScriptViolation(
                    job_id=job_id, job_name=job_name, profile=profile_label,
                    script=script, store=str(store_path),
                    reason=f"MISSING script: {resolved}",
                    via="script-field",
                ))
            elif resolved is not None:
                untracked_rel = validate_script_against_git(resolved, repo)
                if untracked_rel is not None:
                    script_type = "absolute" if Path(script).is_absolute() else "profile-local"
                    violations.append(ScriptViolation(
                        job_id=job_id, job_name=job_name, profile=profile_label,
                        script=script, store=str(store_path),
                        reason=f"untracked script ({script_type}): {untracked_rel}",
                        via="script-field",
                    ))

        # Check command/prompt fields for embedded script references (N2 case).
        for field_name in commands_fields:
            value = job.get(field_name)
            if not isinstance(value, str):
                continue
            if script_token_re is None:
                import re
                script_token_re = re.compile(
                    r"(?:/|~/)[\\w./~+-]+\\.(?:py|sh|bash|pl|rb|js|ts)\\b"
                )
            tokens_seen = set()
            for token in script_token_re.findall(value):
                if token in tokens_seen:
                    continue
                tokens_seen.add(token)
                resolved_ref = Path(token).expanduser().resolve()
                try:
                    resolved_ref.relative_to(repo)
                except ValueError:
                    continue  # outside repo: skip
                if not resolved_ref.exists():
                    violations.append(ScriptViolation(
                        job_id=job_id, job_name=job_name, profile=profile_label,
                        script=token, store=str(store_path),
                        reason=f"COMMAND-REF MISSING script: {resolved_ref}",
                        via=f"command-ref:{field_name}",
                    ))
                else:
                    rel = str(resolved_ref.relative_to(repo))
                    if not is_tracked(rel, repo):
                        violations.append(ScriptViolation(
                            job_id=job_id, job_name=job_name, profile=profile_label,
                            script=token, store=str(store_path),
                            reason=f"COMMAND-REF untracked script: {rel}",
                            via=f"command-ref:{field_name}",
                        ))

    return violations


SCRIPT_TOKEN_RE_STR = r"(?:/|~/)[\\w./~+-]+\\.(?:py|sh|bash|pl|rb|js|ts)\\b"


def get_hermes_cron_stores(repo: Path) -> list[tuple[Path, str, Optional[Path]]]:
    """Find all cron stores relevant to the given repo.

    Returns list of (store_path, profile_label, profile_home_relative_to_repo).
    Mirrors cron_untracked_script_guard.store_paths() logic.
    """
    results: list[tuple[Path, str, Optional[Path]]] = []

    per_profile_dir = repo / "profiles"
    if per_profile_dir.is_dir():
        for p in sorted(per_profile_dir.glob("*")):
            store = p / "cron" / "jobs.json"
            if store.exists() and p.is_dir():
                results.append((store, p.name, p))

    root_store = repo / "cron" / "jobs.json"
    if root_store.exists():
        results.append((root_store, "<root>", None))

    return results


def check_all(repo: Optional[Path] = None) -> tuple[list[ScriptViolation], list[str]]:
    """Main entry point: scan all cron stores and return violations.

    This mirrors the runtime execution path: called from create_job/update_job
    to validate a newly authored script before committing the job to a store.

    Args:
        repo: Explicit repo root. Defaults to resolving from cwd or /home/frank/.hermes.

    Returns:
        (violations, errors) where violations is a list of ScriptViolation
        and errors is a list of error strings.
    """
    if repo is None:
        # Try common locations in order.
        candidate = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes")).expanduser()
        repo = git_repo_root(candidate) or candidate

    errors: list[str] = []
    all_violations: list[ScriptViolation] = []

    for store_path, label, home_rel in get_hermes_cron_stores(repo):
        try:
            viols = check_jobs_store(store_path, repo, label, repo_relative_hermes_home=home_rel)
            all_violations.extend(viols)
        except Exception as exc:
            errors.append(f"failed to check store {store_path}: {exc}")

    return all_violations, errors


# ---------------------------------------------------------------------------
# Author-time API (for use in create_job / update_job)
# ---------------------------------------------------------------------------

def validate_script_for_creation(
    script: str,
    profile_home: Path,
    repo: Path,
    is_no_agent: bool,
) -> tuple[bool, list[str]]:
    """Validate a script path at authoring time.

    This is the function called by create_job() and update_job() before
    persisting a job record. It raises a clear ValueError when the check
    fails, preventing an invalid job from being stored.

    Args:
        script: The script path as provided by the user.
        profile_home: The profile's home directory (HERMES_HOME for its ticker).
        repo: The repository root containing this profile.
        is_no_agent: Whether this is a no_agent job.

    Returns:
        (is_ok, messages). When is_ok is False, messages contains actionable
        guidance including the exact `git add` command.
    """
    resolved, reason = resolve_script_path(profile_home, script)

    if reason == "SCHEDULER-BLOCKED":
        msg = (
            f"Cron job script '{script}' resolves outside the owning profile scripts/ dir.\n"
            f"The scheduler will block execution for this script. Fix the script path "
            f"or ensure the target file exists inside {profile_home}/scripts/\n"
            f"Current resolved path: {resolved}"
        )
        return False, [msg]

    if reason == "MISSING":
        if is_no_agent:
            msg = (
                f"Cron job script '{script}' does not exist on disk: {resolved}\n"
                f"no_agent jobs require a valid script — the script IS the job.\n"
                f"Create the script, then commit it:\n"
                f"  cd {repo} && git add {str(Path(script).relative_to(repo) if str(script).startswith('/') else str(Path(script).relative_to(profile_home)))}\n"
                f"and verify:\n"
                f"  git ls-files --error-unmatch -- {str(Path(script).relative_to(repo))}"
            )
            raise ValueError(msg)
        else:
            msg = (
                f"Cron job script '{script}' does not exist on disk: {resolved}\n"
                f"This will cause a runtime failure. Create the script and commit it:\n"
                f"  cd {repo} && git add ... && git commit -m 'add {script}'\n"
            )
            return False, [msg]

    # Resolve relative to repo for tracking check.
    try:
        rel = str(resolved.relative_to(repo))
    except ValueError:
        # Outside repo — not this repo's responsibility. Accept it.
        return True, []

    if not is_tracked(rel, repo):
        # Build suggestion: if script is relative, it's already relative to profile_home
        if Path(script).is_absolute():
            try:
                suggestion = str(Path(script).relative_to(profile_home))
            except ValueError:
                suggestion = script
        else:
            suggestion = script
        if is_no_agent:
            msg = (
                f"Cron job script '{script}' is NOT tracked by git.\n"
                f"For no_agent jobs, the script IS the job — a missing script means total failure.\n"
                f"Refusing until the script is committed.\n"
                f"Run: cd {repo} && git add {suggestion} && git commit -m 'track {suggestion}'\n"
                f"Verify: git ls-files --error-unmatch -- {rel}"
            )
            raise ValueError(msg)
        else:
            msg = (
                f"Cron job script '{script}' is NOT tracked by git.\n"
                f"The scheduler will find it UNTRACKED at runtime.\n"
                f"Add and commit it:\n"
                f"  cd {repo} && git add {suggestion} && git commit -m 'track {suggestion}'\n"
                f"Verify: git ls-files --error-unmatch -- {rel}"
            )
            return False, [msg]

    return True, []


def validate_script_prompt_refs(
    prompt: str,
    repo: Path,
) -> list[str]:
    """Scan a prompt/command string for embedded script references that are untracked.

    Returns a list of warning messages for any untracked referenced scripts.
    Out-of-repo references are silently ignored (not this repo's asset).
    """
    warnings: list[str] = []
    import re
    matches = re.compile(SCRIPT_TOKEN_RE_STR).findall(prompt)
    seen = set()
    for token in matches:
        if token in seen:
            continue
        seen.add(token)
        resolved = Path(token).expanduser().resolve()
        try:
            rel = str(resolved.relative_to(repo))
        except ValueError:
            continue
        if not resolved.exists():
            continue
        if not is_tracked(rel, repo):
            warnings.append(
                f"Cron job prompt/command references untracked script: {token} "
                f"(git-rel: {rel}). Commit it:\n"
                f"  cd {repo} && git add {token.replace(str(repo), '', 1).lstrip('/')}"
            )
    return warnings
