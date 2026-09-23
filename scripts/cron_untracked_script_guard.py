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

import ast
import hashlib
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
    paths = []
    seen: set[str] = set()
    for p in sorted((REPO / "profiles").glob("*/cron/jobs.json")):
        real = str(p.resolve())
        if real in seen:
            continue
        seen.add(real)
        paths.append((p, p.parts[-3]))
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
    # CONTROL: the post-checkout self-heal hook must be installed and current so
    # branch checkouts cannot silently swap live cron scripts (t_82b9432a).
    # pre-checkout does not fire on this git build (2.43), so the guard lives in
    # post-checkout: after any checkout it restores cron-referenced files from
    # the previous HEAD, making git's native "local changes would be overwritten"
    # protection block further swaps.
    hook = REPO / ".git" / "hooks" / "post-checkout"
    source = REPO / "scripts" / "git-live-cron-postcheckout.sh"
    if not hook.exists() or not os.access(hook, os.X_OK):
        violations.append({
            "job_id": "<control>",
            "job_name": "live-cron-postcheckout-hook",
            "profile": "<root>",
            "script": str(hook),
            "store": "<control>",
            "reason": "CONTROL: post-checkout live-cron self-heal hook missing or not executable",
        })
    elif source.exists():
        try:
            if hashlib.sha256(hook.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                violations.append({
                    "job_id": "<control>",
                    "job_name": "live-cron-postcheckout-hook",
                    "profile": "<root>",
                    "script": str(hook),
                    "store": "<control>",
                    "reason": "CONTROL: post-checkout live-cron self-heal hook source drift "
                              "(installed hook differs from scripts/git-live-cron-postcheckout.sh)",
                })
        except OSError:
            pass
    # CONTROL: the PATH-level git wrapper (t_041d138a) must be installed ahead of
    # /usr/bin/git and match its source, so a worker branch-swap in the live tree is
    # refused even though git 2.43 cannot abort a checkout via hooks. ~/.local/bin is
    # prepended to worker PATH by ~/.fleet_path.sh, so a drift here re-opens the gap.
    wrapper_src = REPO / "scripts" / "git-live-checkout-guard.sh"
    wrapper_install = REPO.parent / ".local" / "bin" / "git"
    if not wrapper_install.exists() or not os.access(wrapper_install, os.X_OK):
        violations.append({
            "job_id": "<control>",
            "job_name": "git-live-checkout-guard-wrapper",
            "profile": "<root>",
            "script": str(wrapper_install),
            "store": "<control>",
            "reason": "CONTROL: live git-checkout guard wrapper missing or not executable "
                      "(expected at ~/.local/bin/git)",
        })
    elif wrapper_src.exists():
        try:
            if hashlib.sha256(wrapper_install.read_bytes()).digest() != hashlib.sha256(wrapper_src.read_bytes()).digest():
                violations.append({
                    "job_id": "<control>",
                    "job_name": "git-live-checkout-guard-wrapper",
                    "profile": "<root>",
                    "script": str(wrapper_install),
                    "store": "<control>",
                    "reason": "CONTROL: live git-checkout guard wrapper source drift "
                              "(installed ~/.local/bin/git differs from scripts/git-live-checkout-guard.sh)",
                })
        except OSError:
            pass
    return violations, sorted(set(v["reason"] for v in violations)), errors


# ---- bundle-runner indirection (t_881f0f42) --------------------------------
# The guard bundle (t_db689c47) condensed whole job groups into four tick jobs:
#
#   guard-bundle-tick-* -> guard_bundle_run.sh -> report-to-board.py
#     -> <profile_home>/scripts/cron_guard_bundle_runner.py
#     -> <profile_home>/scripts/<check>
#
# Neither the runner nor any manifest check is named by a cron store `script`
# field, so the store scan in referenced_paths() cannot see them: t_25086e48
# protected exactly ONE of them (the guard's own executed copy, via
# live-critical-paths.txt) and left the runner plus the other 43 checks outside
# the post-checkout self-heal set. A live-tree checkout could therefore silently
# revert any of them to its HEAD blob (stale for several — see t_f20b594a).
BUNDLE_RUNNER_NAME = "cron_guard_bundle_runner.py"


def bundle_manifest_scripts(runner: Path) -> list[str]:
    """Executed check file names from a guard-bundle runner's CHECKS manifest.

    Parsed with `ast`, not a regex: manifest values legitimately contain calls
    and dict lookups (`_min(5)`, `_manifest_boards()`), and only the literal
    `script` key carries the executed file name. Deliberately the same parse as
    profile_script_drift_watch.bundle_manifest_scripts (t_25086e48), so the
    durability half and the detection half agree on what actually executes.
    """
    try:
        tree = ast.parse(runner.read_text(errors="replace"))
    except Exception:
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "CHECKS" for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not isinstance(value, ast.Dict):
                continue
            for vkey, vvalue in zip(value.keys, value.values):
                if (
                    isinstance(vkey, ast.Constant)
                    and vkey.value == "script"
                    and isinstance(vvalue, ast.Constant)
                    and isinstance(vvalue.value, str)
                ):
                    out.append(vvalue.value)
    return out


def bundle_referenced_paths() -> list[Path]:
    """The bundle runner itself plus every check it executes (t_881f0f42).

    Resolved exactly like the scheduler: a bare manifest name becomes
    <profile_home>/scripts/<name>, and anything that escapes the owning profile
    scripts dir (the scheduler's SCHEDULER-BLOCKED condition) is skipped — it can
    never execute. Paths are returned whether or not they exist, like the store
    scan: a file a checkout just deleted is exactly what the hook must restore.
    """
    paths: list[Path] = []
    seen_runners: set[str] = set()
    for runner in sorted((REPO / "profiles").glob(f"*/scripts/{BUNDLE_RUNNER_NAME}")):
        real = os.path.realpath(runner)
        if real in seen_runners:
            continue  # symlink alias — dedupe
        seen_runners.add(real)
        profile_home = runner.parents[1]
        try:
            paths.append(runner.resolve())
            paths[-1].relative_to(REPO)
        except ValueError:
            paths.pop()  # outside REPO: a checkout here cannot affect it
        for script in bundle_manifest_scripts(runner):
            try:
                resolved, _ = resolve_like_scheduler(profile_home, script)
            except ValueError:
                continue  # scheduler-blocked: never a live path
            try:
                resolved.relative_to(REPO)
            except ValueError:
                continue
            paths.append(resolved)
    return paths


def referenced_paths() -> tuple[list[Path], list[str]]:
    """Resolve every file referenced by an enabled cron job to an absolute path.

    Used by the post-checkout live-cron self-heal hook (t_82b9432a): the hook
    restores these files from the previous HEAD after any checkout so a branch
    swap can never silently change what the fleet executes.

    Three sources, unioned (t_881f0f42 added the third):
      1. every enabled cron job's `script` / command-embedded path,
      2. guard-bundle-indirected executed copies (the runner + its CHECKS
         manifest; no cron store names them),
      3. live-critical-paths.txt (seat-invoked launchers, not cron-wired).

    Only paths inside REPO are returned — a checkout in this repo cannot affect
    files outside it. Paths are returned whether or not they currently exist:
    a referenced file that a checkout just deleted is exactly what the hook must
    restore.
    """
    stores, errors = load_stores()
    paths: list[Path] = []
    for store in stores:
        profile = store["profile"]
        profile_home = REPO if profile == "<root>" else REPO / "profiles" / profile
        for job in store["jobs"]:
            script = job["script"]
            try:
                resolved, _ = resolve_like_scheduler(profile_home, script)
            except ValueError:
                continue  # scheduler-blocked; never a live path
            try:
                resolved.relative_to(REPO)
            except ValueError:
                continue
            paths.append(resolved)
        for ref in store.get("refs", []):
            resolved = Path(ref["token"]).expanduser().resolve()
            try:
                resolved.relative_to(REPO)
            except ValueError:
                continue
            paths.append(resolved)
    # Bundle-runner-indirected executed copies (t_881f0f42). Added AFTER the
    # store scan so a file named both ways is still protected once; the final
    # dedupe in main()/callers collapses the overlap.
    paths.extend(bundle_referenced_paths())
    # Static live-critical manifest (t_041d138a, 2026-08-11): seat-invoked scripts
    # that are NOT cron-wired but must survive a worker branch-switch (the gap that
    # wiped the experiment-factory launchers). One repo-relative path per line, '#'
    # comments ignored. Missing manifest is fine (no extra protection, no error).
    manifest = REPO / "live-critical-paths.txt"
    if manifest.exists():
        try:
            for line in manifest.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                resolved = (REPO / line).expanduser().resolve()
                try:
                    resolved.relative_to(REPO)
                except ValueError:
                    continue
                paths.append(resolved)
        except OSError as exc:
            errors.append(f"live-critical-paths manifest unreadable: {exc}")
    return paths, errors


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--referenced-paths":
        paths, errors = referenced_paths()
        if errors:
            print(json.dumps({
                "healthy": False,
                "operational_error": errors,
            }, indent=2))
            return 2
        for p in sorted({str(p) for p in paths}):
            print(p)
        return 0

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
