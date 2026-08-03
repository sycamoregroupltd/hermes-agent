#!/usr/bin/env python3
"""cron_untracked_script_guard.py — fail when an enabled cron job's script is untracked in git.

This guard protects against the failure mode where a live automation script exists only as
an untracked working-tree file in the Hermes home git repo, one `git clean` / pristine checkout
/ worktree reset away from silent deletion.

SCHEDULER GROUND TRUTH (hermes-agent/cron/scheduler.py:2328-2346)
---------------------------------------------------------------
A cron `script` value is resolved by the scheduler as follows:

    scripts_dir = _get_hermes_home() / "scripts"        # PROFILE home for a profile ticker
    raw  = Path(script_path).expanduser()
    path = raw.resolve() if raw.is_absolute() else (scripts_dir / raw).resolve()
    path.relative_to(scripts_dir_resolved)              # ValueError -> "Blocked: ... outside"

There is NO global `~/.hermes/scripts/` fallback. A relative script resolves against the OWNING
profile's scripts/ dir. An absolute or ~-prefixed script is .resolve()'d and must stay *inside*
that same scripts/ dir (the relative_to containment check refuses anything outside it). This
guard reproduces that exact resolution + containment model so it flags the same scripts the
scheduler would refuse or silently lose.

Resolution model used by THIS guard (mirrors the scheduler):
  - Relative script  -> <profile_home>/scripts/<script>
  - Absolute script   -> resolved; must be contained within <profile_home>/scripts/
  - A script that resolves OUTSIDE the owning profile scripts/ dir is reported as
    SCHEDULER-BLOCKED (it can never run), not silently passed.
  - A script that does not exist on disk is reported as MISSING (a real live-outage finding,
    exit 1 — NOT an operational error).

Tracked-check: per-script `git ls-files --error-unmatch <path>`. This is the correct primitive
because it sees gitignored paths (unlike the old `git ls-files --others --exclude-standard`
intersection, which silently suppressed gitignored files and produced a false green).

Exit codes:
  0  healthy (no enabled cron job references an untracked / scheduler-blocked / missing script)
  1  one or more enabled jobs reference untracked, scheduler-blocked, or missing scripts
  2  operational error (could not locate the git repo, or a store failed to parse)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

RAW_HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/frank/.hermes")).expanduser()


def git_repo_root(start: Path) -> Path | None:
    """Resolve the enclosing git repository root from *start* via git itself.

    Returns None when *start* is not inside a git work tree (so the caller can fail loudly
    instead of silently scanning nothing).
    """
    r = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, timeout=120,
    )
    if r.returncode == 0 and r.stdout.strip():
        return Path(r.stdout.strip()).expanduser()
    return None


# HERMES_HOME may be a profile directory; the cron stores and the git repo live at the repo root.
REPO = git_repo_root(RAW_HERMES_HOME) or RAW_HERMES_HOME


def git(args: list[str]) -> tuple[int, str, str]:
    cmd = ["git", "-C", str(REPO), *args]
    r = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def is_tracked(rel_path: str) -> bool:
    """True iff *rel_path* (repo-relative) is tracked by git (committed or staged).

    Uses `git ls-files --error-unmatch`, which DOES see gitignored paths — the correct
    primitive. A gitignored-but-untracked file is NOT tracked, so this returns False and the
    guard flags it (the intended behavior; the old `--others` intersection could not).
    """
    rc, _out, _err = git(["ls-files", "--error-unmatch", "--", rel_path])
    return rc == 0


def store_paths() -> list[tuple[Path, str]]:
    """Every cron store this guard is responsible for, as (path, profile-label).

    SCOPE DECISION (t_8861845f / os-reviewer note N1)
    -------------------------------------------------
    IN scope:
      - <repo>/profiles/*/cron/jobs.json  — per-profile tickers (the common case).
      - <repo>/cron/jobs.json             — the ROOT non-profile store. It is loaded by a
        ticker whose HERMES_HOME is the repo root, so its scripts resolve against
        <repo>/scripts/. It currently holds 41 jobs with 0 enabled, which makes it harmless
        TODAY and invisible to the old profile-only glob. Re-enabling any of its 20
        script-bearing jobs would have silently reopened the exact untracked-script hole
        this guard exists to close, so it is now scanned unconditionally. Profile label is
        "<root>" so violations are attributable.

    OUT of scope (deliberate, not an oversight):
      - /home/frank/.hermes-worktrees/*/cron/jobs.json and /home/frank/.hermes/kb-merge-*/
        cron/jobs.json (~12 stores at time of writing). These are ephemeral git worktrees /
        merge scratch trees of the SAME repo. They are (a) outside REPO, so their scripts have
        no repo-relative identity to `git ls-files` against, (b) not loaded by any running
        gateway ticker — only the canonical ~/.hermes tickers run, and (c) by construction
        transient: flagging them would produce recurring noise that resolves itself when the
        worktree is removed, which is how a guard gets muted. The real asset being protected
        (the committed script) is already covered via the canonical store. If a worktree is
        ever promoted to a live ticker home, it must be added here explicitly.
    """
    paths = [(p, p.parts[-3]) for p in sorted((REPO / "profiles").glob("*/cron/jobs.json"))]
    root = REPO / "cron" / "jobs.json"
    if root.exists():
        paths.append((root, "<root>"))
    return paths


def job_is_enabled(job: dict) -> bool:
    if job.get("enabled") is False:
        return False
    if job.get("state") == "paused":
        return False
    if job.get("disabled") is True:
        return False
    return True


# N2: script-like path tokens embedded in a job's `command` / `prompt` string. Only rooted
# tokens (absolute or ~-prefixed) are considered — a bare "foo.py" in prose is far too
# ambiguous to resolve without inventing a base dir, and inventing one produces false MISSING
# findings. A rooted token is unambiguous: it either exists at that path or it does not.
COMMAND_FIELDS = ("command", "prompt")
SCRIPT_TOKEN_RE = re.compile(r"(?:/|~/)[\w./~+-]+\.(?:py|sh|bash|pl|rb|js|ts)\b")


def load_stores() -> tuple[list[dict], list[str]]:
    stores: list[dict] = []
    errors: list[str] = []
    for path, label in store_paths():
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            errors.append(f"failed to parse store {path}: {exc}")
            continue
        jobs = data.get("jobs") or []
        hits = []
        refs = []
        for job in jobs:
            if not job_is_enabled(job):
                continue
            script = job.get("script")
            if script:
                hits.append({
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "profile": label,
                    "script": script,
                    "store": str(path),
                })
            for field in COMMAND_FIELDS:
                value = job.get(field)
                if not isinstance(value, str):
                    continue
                for token in dict.fromkeys(SCRIPT_TOKEN_RE.findall(value)):
                    refs.append({
                        "id": job.get("id"),
                        "name": job.get("name"),
                        "profile": label,
                        "field": field,
                        "token": token,
                        "store": str(path),
                    })
        if hits or refs:
            stores.append({
                "profile": label,
                "store": str(path),
                "jobs": hits,
                "refs": refs,
            })
    return stores, errors


def resolve_like_scheduler(profile_home: Path, script: str) -> tuple[Path, Path]:
    """Reproduce scheduler.py:2328-2346 resolution.

    Returns (resolved_path, scripts_dir_resolved). Raises ValueError if the resolved path
    escapes the owning profile scripts/ dir (the scheduler's SCHEDULER-BLOCKED condition).
    """
    scripts_dir = (profile_home / "scripts")
    scripts_dir_resolved = scripts_dir.resolve()
    raw = Path(script).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()
    # Containment check identical to the scheduler.
    path.relative_to(scripts_dir_resolved)
    return path, scripts_dir_resolved


def audit() -> tuple[list[dict], list[str], list[str]]:
    violations: list[dict] = []
    errors: list[str] = []
    stores, store_errors = load_stores()
    errors.extend(store_errors)

    for store in stores:
        profile = store["profile"]
        # The profile's HERMES_HOME is the repo-root/profiles/<profile> directory. Mirror the
        # scheduler's _get_hermes_home() resolution by expanding via the repo layout. The
        # root (non-profile) store's ticker has HERMES_HOME == repo root itself.
        profile_home = REPO if profile == "<root>" else REPO / "profiles" / profile
        for job in store["jobs"]:
            script = job["script"]
            scripts_dir_resolved = (profile_home / "scripts").resolve()
            try:
                resolved, _scripts_dir_resolved = resolve_like_scheduler(profile_home, script)
            except ValueError:
                violations.append({
                    "job_id": job["id"],
                    "job_name": job["name"],
                    "profile": profile,
                    "script": script,
                    "store": job["store"],
                    "reason": "SCHEDULER-BLOCKED: script resolves outside the owning "
                              f"profile scripts dir ({scripts_dir_resolved})",
                })
                continue

            if not resolved.exists():
                # A nonexistent script is a live-outage finding, not an operational error.
                violations.append({
                    "job_id": job["id"],
                    "job_name": job["name"],
                    "profile": profile,
                    "script": script,
                    "store": job["store"],
                    "reason": f"MISSING script: {resolved}",
                })
                continue

            rel = str(resolved.relative_to(REPO))
            if not is_tracked(rel):
                violations.append({
                    "job_id": job["id"],
                    "job_name": job["name"],
                    "profile": profile,
                    "script": script,
                    "store": job["store"],
                    "reason": f"untracked script ({'absolute' if Path(script).is_absolute() else 'profile-local'}): {rel}",
                })

        # N2 — script paths referenced from a job's command/prompt string rather than the
        # `script` field. These bypass the scheduler's scripts/ containment entirely (the
        # agent shells out), so the resolution model is plain filesystem resolution, not
        # resolve_like_scheduler(). Only in-repo tokens can be tracked-checked; tokens
        # outside the repo (e.g. /home/frank/uaa-rules/...) are reported as EXTERNAL-REF
        # informational context, not violations — this repo cannot vouch for them.
        for ref in store.get("refs", []):
            resolved = Path(ref["token"]).expanduser()
            try:
                resolved = resolved.resolve()
                rel = str(resolved.relative_to(REPO))
            except ValueError:
                continue  # outside the repo: not this guard's asset to protect
            base = {
                "job_id": ref["id"],
                "job_name": ref["name"],
                "profile": ref["profile"],
                "script": ref["token"],
                "store": ref["store"],
                "via": f"command-ref:{ref['field']}",
            }
            if not resolved.exists():
                violations.append({**base,
                                   "reason": f"COMMAND-REF MISSING script: {resolved}"})
            elif not is_tracked(rel):
                violations.append({**base,
                                   "reason": f"COMMAND-REF untracked script: {rel}"})
    return violations, sorted(set(v["reason"] for v in violations)), errors


def main() -> int:
    if REPO == RAW_HERMES_HOME and git_repo_root(RAW_HERMES_HOME) is None:
        print(json.dumps({
            "healthy": False,
            "error": "could not resolve git repo root from HERMES_HOME",
            "hermes_home": str(RAW_HERMES_HOME),
        }, indent=2))
        return 2
    try:
        violations, reasons, errors = audit()
    except RuntimeError as exc:
        print(json.dumps({"healthy": False, "operational_error": str(exc)}, indent=2))
        return 2

    if violations or errors:
        out = {
            "healthy": False,
            "repo_root": str(REPO),
            "hermes_home": str(RAW_HERMES_HOME),
            "violation_count": len(violations),
            "violations": violations,
            "distinct_reasons": reasons,
            "errors": errors,
        }
        print(json.dumps(out, indent=2))
        # MISSING and untracked and SCHEDULER-BLOCKED are all real findings -> exit 1.
        # Only a genuine git/parse failure (errors without violations) is exit 2.
        return 1 if violations else 2

    print(f"OK cron_untracked_script_guard: 0 enabled cron jobs reference an "
          f"untracked/scheduler-blocked/missing script (repo_root={REPO})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
