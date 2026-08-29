#!/usr/bin/env python3
"""
Runtime-faithful cron skill pin preflight resolver.

Resolves skill pins exactly as the Hermes cron executor does at load time:
- Reads the owning profile's config.yaml to find skills.external_dirs
- Walks the profile-local skills/ tree AND all external_dirs recursively
- Matches by DIRECTORY NAME (same as _find_skill in the Hermes runtime)
- Applies the same exclusion rules as is_excluded_skill_path
- Reports a pin MISSING only when no SKILL.md with that name is found

CLI modes:
    --job-id <id> --jobs-file <path>   Resolve pins for a job in the given jobs.json
    --profile <p> --pins n1 n2 ...     Resolve named pins under the given profile
    --self-test                         Run regression guard against known job IDs
    --capability-assert <flag>         Verify THIS resolver advertises <flag> via
                                       --help before a caller relies on it. Exit 0
                                       if present; exit 2 (with a MECHANISM-GAP
                                       routing diagnostic) if missing. Guards the
                                       governor FIRST-RUN PREFLIGHT against a
                                       per-task branch rotation silently dropping a
                                       required flag (the 2026-08-01 --script-check
                                       rollback, which crashed the governor with an
                                       opaque argparse "unrecognized arguments" exit 2).

Exit codes:
    0   All pins resolved / capability asserted
    1   One or more pins missing (a real per-job failure — routable preflight card)
    2   Usage error OR capability-assert miss (MECHANISM-GAP — the resolver binary
        itself lacks a flag the governor depends on; routable as a mechanism
        regression card, NOT as a per-job MISSING-script card)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


HERMES_ROOT = Path("/home/frank/.hermes")
PROFILES_DIR = HERMES_ROOT / "profiles"
GLOBAL_SKILLS = HERMES_ROOT / "skills"


def resolve_job_script(job_id: str, jobs_file: str) -> dict:
    """Runtime-faithful cron *script* existence resolver (mirrors scheduler._run_job_script).

    The governor Beat-4 FIRST-RUN PREFLIGHT previously checked the job ``script``
    field with a shallow existence scan against the wrong directory
    (``~/.hermes/scripts`` / global / stale worktrees), which emitted
    ``script_exists=False`` for jobs whose script actually lives in the owning
    profile's ``HERMES_HOME/scripts`` (e.g. jarvis cron 7ad0e11f7790 ->
    ``prune-default-state-db.py`` at ``profiles/jarvis/scripts/``).

    This resolves the script exactly the way the scheduler does at tick time:
      1. owning profile is inferred from the jobs-file path
         ``profiles/<name>/cron/jobs.json`` (same as _run_job_script's
         ``get_hermes_home()`` for a profile-scoped gateway).
      2. relative script -> ``<profile_home>/scripts/<script>``
      3. absolute / ``~`` script -> resolved, but must stay *within*
         ``<profile_home>/scripts`` (traversal guard, same as the runtime).
    """
    profile = infer_profile_from_jobs_file(jobs_file)
    if not profile:
        return {
            "job_id": job_id,
            "status": "USAGE-ERROR",
            "resolved_path": None,
            "error": "cannot infer owning profile from jobs-file path",
        }

    with open(jobs_file) as f:
        data = json.load(f)
    job = None
    for j in data.get("jobs", []):
        if j.get("id") == job_id:
            job = j
            break
    if job is None:
        return {
            "job_id": job_id,
            "status": "JOB-NOT-FOUND",
            "resolved_path": None,
            "error": f"no job {job_id} in {jobs_file}",
        }

    script = job.get("script")
    if not script:
        return {
            "job_id": job_id,
            "status": "NO-SCRIPT",
            "resolved_path": None,
            "error": "job has no 'script' field (agent-driven job)",
        }

    profile_home = PROFILES_DIR / profile
    scripts_dir = (profile_home / "scripts")
    scripts_dir_resolved = scripts_dir.resolve()

    raw = Path(script).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (scripts_dir / raw).resolve()

    try:
        path.relative_to(scripts_dir_resolved)
    except ValueError:
        return {
            "job_id": job_id,
            "profile": profile,
            "status": "BLOCKED-ESCAPE",
            "script": script,
            "resolved_path": str(path),
            "error": "script resolves outside the profile scripts directory (traversal guard)",
        }

    if path.is_file():
        return {
            "job_id": job_id,
            "profile": profile,
            "status": "RESOLVED",
            "script": script,
            "resolved_path": str(path),
        }
    return {
        "job_id": job_id,
        "profile": profile,
        "status": "MISSING",
        "script": script,
        "resolved_path": str(path),
        "error": "script not found at the owning profile's scripts dir",
    }


def run_script_self_test() -> int:
    """Regression guard: a job whose script lives only in the owning profile
    scripts dir must RESOLVE (the exact false-alarm class from 7ad0e11f7790).
    """
    jobs_file = str(PROFILES_DIR / "jarvis" / "cron" / "jobs.json")

    cases = [
        ("7ad0e11f7790", "state-db-retention", "prune-default-state-db.py", "RESOLVED"),
    ]
    all_ok = True
    for job_id, job_name, _script, expected in cases:
        res = resolve_job_script(job_id, jobs_file)
        ok = res.get("status") == expected
        all_ok = all_ok and ok
        print(f"[{job_name}] ({job_id}): {res.get('status')}"
              + (f" -> {res.get('resolved_path')}" if res.get("resolved_path") else ""))
        if not ok:
            print(f"  ** FAIL: expected {expected}, got {res.get('status')} **")
    # Negative control: a job whose script genuinely does not exist must MISS.
    # Use a synthetic profile tree so infer_profile_from_jobs_file resolves a
    # real profile home (the same path-shape the scheduler uses at tick time).
    import tempfile
    with tempfile.TemporaryDirectory(prefix="preflight-script-test-") as tmp:
        synthetic_root = Path(tmp) / "profiles" / "synthetic"
        (synthetic_root / "scripts").mkdir(parents=True)
        synthetic = synthetic_root / "cron" / "jobs.json"
        synthetic.parent.mkdir(parents=True)
        synthetic.write_text(json.dumps({
            "jobs": [{"id": "deadbeef00", "name": "synthetic-missing",
                      "script": "this_script_does_not_exist_xyz.py"}]
        }))
        res = resolve_job_script("deadbeef00", str(synthetic))
        ok = res.get("status") == "MISSING"
        all_ok = all_ok and ok
        print(f"[synthetic-missing] (deadbeef00): {res.get('status')}"
              + (f" -> {res.get('resolved_path')}" if res.get("resolved_path") else ""))
        if not ok:
            print("  ** FAIL: expected MISSING for synthetic non-existent script **")
    if all_ok:
        print("SCRIPT SELF-TEST PASS (exit 0)")
    else:
        print("SCRIPT SELF-TEST FAIL (exit 1)")
    return 0 if all_ok else 1


def run_prompt_self_test() -> int:
    """Regression guard: the Beat-4 FIRST-RUN PREFLIGHT prompt must keep the
    script-resolution resolver clause and exit-semantics sentence, and must
    not regress to the old shallow-scan wording.

    Includes explicit positive/negative fixture controls so a future maintainer
    can demonstrate the assertions are live: toggle either fixture and rerun to
    force a deliberate failure or pass.
    """
    expected = (
        "--script-check --job-id <id> --jobs-file"
    )
    # The 2026-08-01 hardening: the FIRST-RUN PREFLIGHT must assert the resolver
    # capability FIRST, before invoking --script-check. A missing capability must
    # route a MECHANISM-GAP card (exit 2), never be mistaken for a per-job failure
    # (exit 1) or crash the governor with an opaque argparse exit 2.
    capability_clause = (
        "capability-assert script-check"
    )
    forbidden = "script path exists, workdir exists"
    exit_semantics = (
        "Treat exit 0 (RESOLVED/NO-SCRIPT) as PASS; exit 1 "
        "(MISSING/BLOCKED-ESCAPE) is the only condition that may route "
        "a first-run preflight card."
    )
    mechanism_gap = "MECHANISM-GAP"
    # Resolve the jobs.json relative to THIS resolver's checkout (repo-faithful),
    # so the guard validates the code under test rather than a hardcoded live
    # path. Falls back to the absolute HERMES_ROOT path when the relative form is
    # not co-located (e.g. resolver imported from an unusual location).
    def _candidate_jobs_paths():
        here = Path(__file__).resolve()
        # Walk up to a dir that contains profiles/jarvis/cron/jobs.json
        for parent in [here, *here.parents]:
            cand = parent / "profiles" / "jarvis" / "cron" / "jobs.json"
            if cand.is_file():
                yield str(cand)
        yield str(PROFILES_DIR / "jarvis" / "cron" / "jobs.json")
    expected_jobs_path = next(_candidate_jobs_paths())
    expected_job_id = "e51c9e2fa5df"

    def load_prompt(path: str, job_id: str):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for job in data.get("jobs", []):
            if job.get("id") == job_id:
                return job.get("prompt") or ""
        return None

    prompt = load_prompt(expected_jobs_path, expected_job_id)
    if prompt is None:
        print(f"PROMPT SELF-TEST FAIL: job {expected_job_id} not found")
        return 1

    # Fixture controls: set any True to force the opposite outcome and prove
    # the assertions are active. Keep all False for the real live check.
    force_missing_resolver = False
    force_forbidden_present = False
    force_missing_exit_semantics = False
    force_missing_capability_clause = False

    def force_fail(flag: bool) -> bool:
        return not flag

    ok_all = True
    ok = force_fail(force_missing_resolver) and (expected in prompt)
    print(f"[expected-clause] {'PASS' if ok else 'FAIL'}")
    ok_all = ok_all and ok

    ok = force_fail(force_forbidden_present) and (forbidden not in prompt)
    print(f"[forbidden-wording] {'PASS' if ok else 'FAIL'}")
    ok_all = ok_all and ok

    ok = force_fail(force_missing_exit_semantics) and (exit_semantics in prompt)
    print(f"[exit-semantics] {'PASS' if ok else 'FAIL'}")
    ok_all = ok_all and ok

    ok = force_fail(force_missing_capability_clause) and (capability_clause in prompt)
    print(f"[capability-assert-clause] {'PASS' if ok else 'FAIL'}")
    ok_all = ok_all and ok

    ok = force_fail(force_missing_capability_clause) and (mechanism_gap in prompt)
    print(f"[mechanism-gap-verdict] {'PASS' if ok else 'FAIL'}")
    ok_all = ok_all and ok

    if ok_all:
        print("PROMPT SELF-TEST PASS (exit 0)")
    else:
        print("PROMPT SELF-TEST FAIL (exit 1)")
    return 0 if ok_all else 1


def run_prompt_self_test_negative_fixture() -> int:
    """Negative control: a fixture prompt using the old wording MUST fail."""
    forbidden = "script path exists, workdir exists"
    fixture = (
        "First-run preflight check: verify script path exists, workdir exists, "
        "then skill-pin resolver.\n"
    )
    failed = forbidden in fixture
    if not failed:
        print("NEGATIVE FIXTURE FAIL: assertion did not fail on old wording")
    else:
        print("NEGATIVE FIXTURE PASS: assertion failed as expected")
    return 0 if failed else 1


def run_capability_assert(flag: str, resolver_path: str = None) -> int:
    """Verify THIS resolver advertises ``flag`` before a caller relies on it.

    A per-task branch rotation of the live Hermes repo can silently roll the
    resolver back to a version that lacks a flag the governor depends on (the
    FIRST-RUN PREFLIGHT ``--script-check`` rollback of 2026-08-01). Without this
    guard the governor's invocation fails with argparse "unrecognized arguments"
    exit 2 — an opaque mechanism regression indistinguishable from a usage error.

    This inspects the resolver's OWN ``--help`` output so it is faithful to the
    exact artifact on disk (no hard-coded flag list to drift). Exit 0 if the flag
    is advertised; exit 2 with a MECHANISM-GAP routing diagnostic if it is not.
    The non-zero exit (2) is DISTINCT from the pin-resolution exit 1, so callers
    can route a *mechanism-missing* preflight card rather than a false
    MISSING-script (exit 1) card.
    """
    import subprocess
    target = resolver_path or __file__
    try:
        out = subprocess.run(
            [sys.executable, target, "--help"],
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception as e:  # pragma: no cover - defensive
        print(json.dumps({
            "status": "CAPABILITY-CHECK-ERROR",
            "flag": flag,
            "error": f"could not invoke resolver --help: {e}",
        }))
        return 2
    tokens = out.split()
    if f"--{flag.lstrip('-')}" in tokens:
        print(json.dumps({"status": "CAPABILITY-OK", "flag": flag}))
        return 0
    print(json.dumps({
        "status": "MECHANISM-GAP",
        "flag": flag,
        "diagnosis": (
            "Resolver on disk does not advertise the required --%s flag. A "
            "per-task branch rotation likely rolled the live resolver back to a "
            "version without it. Do NOT invoke the resolver with --%s (it would "
            "crash with argparse 'unrecognized arguments' exit 2 — a silent "
            "mechanism regression, not a per-job failure). Route a FIRST-RUN "
            "PREFLIGHT MECHANISM-GAP card reporting the resolver regression and "
            "restore the resolver from origin before relying on it." % (flag, flag)
        ),
    }))
    return 2


def run_guard_regression_self_test() -> int:
    """Regression guard for the capability-assert mechanism itself.

    Positive control: the live resolver DOES advertise ``--script-check``, so a
    capability assert must PASS (exit 0).

    Negative control (the real bug from 2026-08-01): a resolver WITHOUT
    ``--script-check`` (the pin-only rollback) must make the capability assert
    FAIL (exit 2) and emit MECHANISM-GAP — NOT an argparse "unrecognized
    arguments" crash. This proves the guard converts the silent regression into
    a routable preflight condition, satisfying acceptance criterion (2).
    """
    import subprocess
    import tempfile
    live_ok = run_capability_assert("script-check") == 0

    # Negative control: reconstruct the pin-only resolver body (no --script-check)
    # that the 2026-08-01 branch rotation dropped back to.
    pin_only_body = (
        "import argparse, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--job-id')\n"
        "p.add_argument('--jobs-file')\n"
        "p.add_argument('--profile')\n"
        "p.add_argument('--pins', nargs='*')\n"
        "p.add_argument('--self-test', action='store_true')\n"
        "p.parse_args()\n"
    )
    all_ok = True
    with tempfile.TemporaryDirectory(prefix="preflight-pinonly-") as tmp:
        pin_path = Path(tmp) / "pin_only_resolver.py"
        pin_path.write_text(pin_only_body)
        try:
            res = subprocess.run(
                [sys.executable, str(pin_path), "--help"],
                capture_output=True, text=True, check=True,
            ).stdout or ""
        except Exception as e:
            print(f"[pin-only --help] ERROR: {e}")
            res = ""
        pin_has_flag = "--script-check" in res.split()
        # The bug class: pin-only resolver must NOT have the flag, and invoking
        # it with --script-check must crash with argparse exit 2 (the silent failure).
        crashed = False
        try:
            subprocess.run(
                [sys.executable, str(pin_path), "--script-check", "--job-id", "x",
                 "--jobs-file", "y"],
                capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as e:
            crashed = (e.returncode == 2)
        print(f"[pin-only has --script-check] {pin_has_flag} (expected False)")
        print(f"[pin-only invoked with --script-check crashes exit 2] {crashed} (expected True)")
        # The capability assert run against THIS pin-only file must FAIL (exit 2)
        # and emit MECHANISM-GAP instead of the opaque argparse crash. This is the
        # actual guard path the governor will use.
        pin_assert_rc = run_capability_assert("script-check", str(pin_path))
        pin_assert_ok = (pin_assert_rc == 2)
        print(f"[pin-only capability-assert exit] {pin_assert_rc} (expected 2)")
        assert_ok = (not pin_has_flag) and crashed and pin_assert_ok
        all_ok = all_ok and assert_ok
        print(f"[pin-only capability-guard catches the rollback with MECHANISM-GAP] {assert_ok} (expected True)")

    print(f"[live resolver advertises --script-check] {live_ok} (expected True)")
    all_ok = all_ok and live_ok
    if all_ok:
        print("GUARD REGRESSION SELF-TEST PASS (exit 0)")
    else:
        print("GUARD REGRESSION SELF-TEST FAIL (exit 1)")
    return 0 if all_ok else 1


EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive",
    ".venv", "venv", "node_modules",
    "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
))


def is_excluded_skill_path(path: Path) -> bool:
    """True if path should be skipped by active skill scanners.

    Matches the Hermes runtime's is_excluded_skill_path logic:
    checks every path component against EXCLUDED_SKILL_DIRS.
    """
    parts = path.parts
    return any(part in EXCLUDED_SKILL_DIRS for part in parts)


def parse_frontmatter_name(skill_md_path: Path) -> str | None:
    """Extract `name:` from YAML frontmatter in a SKILL.md file."""
    try:
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # Capture YAML frontmatter between --- delimiters
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None

    frontmatter = m.group(1)
    # Match `name: <value>` (possibly with quotes)
    nm = re.search(r'^name:\s*["\']?([^\s"\'#]+)', frontmatter, re.MULTILINE)
    if nm:
        return nm.group(1)

    return None


def build_skill_index(profile_name: str) -> dict[str, Path]:
    """
    Build a name→path index for the given profile, walking:
    1. The profile-local skills/ tree: profiles/<name>/skills/
    2. Each external_dir from the profile's config.yaml

    Mirrors the Hermes runtime _find_skill logic:
    - Uses rglob("SKILL.md")
    - Applies is_excluded_skill_path (skips .archive etc.)
    - Matches by DIRECTORY NAME (skill_md.parent.name), NOT frontmatter
    """
    index: dict[str, Path] = {}
    searched_dirs: list[Path] = []

    profile_dir = PROFILES_DIR / profile_name
    profile_skills = profile_dir / "skills"
    if profile_skills.is_dir():
        searched_dirs.append(profile_skills)
        for skill_md in profile_skills.rglob("SKILL.md"):
            if is_excluded_skill_path(skill_md):
                continue
            name = skill_md.parent.name
            if name not in index:
                index[name] = skill_md

    # Read config.yaml for external_dirs
    config_path = profile_dir / "config.yaml"
    if config_path.is_file():
        try:
            import yaml
            cfg = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="replace"))
            ext_dirs = (cfg or {}).get("skills", {}).get("external_dirs", [])
            if ext_dirs and isinstance(ext_dirs, list):
                for ext_dir_str in ext_dirs:
                    ext_path = Path(ext_dir_str)
                    if ext_path.is_dir():
                        searched_dirs.append(ext_path)
                        for skill_md in ext_path.rglob("SKILL.md"):
                            if is_excluded_skill_path(skill_md):
                                continue
                            name = skill_md.parent.name
                            if name not in index:
                                index[name] = skill_md
        except ImportError:
            # Fallback: walk the global skills dir if yaml is unavailable
            if GLOBAL_SKILLS.is_dir():
                for skill_md in GLOBAL_SKILLS.rglob("SKILL.md"):
                    if is_excluded_skill_path(skill_md):
                        continue
                    name = skill_md.parent.name
                    if name not in index:
                        index[name] = skill_md
        except Exception:
            pass

    return index


def resolve_job_pins(job_id: str, jobs_file: str, index: dict[str, Path]) -> list[dict]:
    """Resolve all skills pins for a given job ID. Returns list of result dicts."""
    with open(jobs_file) as f:
        data = json.load(f)

    job = None
    for j in data.get("jobs", []):
        if j.get("id") == job_id:
            job = j
            break

    if job is None:
        return [{"name": f"JOB-NOT-FOUND:{job_id}", "status": "MISSING", "path": None}]

    pins = job.get("skills") or []
    if not pins:
        return [{"name": "(no pins)", "status": "OK", "path": None}]

    results = []
    for pin in pins:
        if pin in index:
            results.append({"name": pin, "status": "OK", "path": str(index[pin])})
        else:
            results.append({"name": pin, "status": "MISSING", "path": None})
    return results


def resolve_pins(pins: list[str], index: dict[str, Path]) -> list[dict]:
    """Resolve a list of pin names against an index."""
    results = []
    for pin in pins:
        if pin in index:
            results.append({"name": pin, "status": "OK", "path": str(index[pin])})
        else:
            results.append({"name": pin, "status": "MISSING", "path": None})
    return results


def infer_profile_from_jobs_file(jobs_file: str) -> str | None:
    """Infer owning profile from jobs-file path pattern: <root>/profiles/<name>/cron/jobs.json.

    Path-shape based (not anchored to the canonical HERMES_ROOT) so synthetic
    profile trees in self-tests resolve identically. The scheduler's
    ``get_hermes_home()`` for a profile-scoped gateway is exactly
    ``profiles/<name>``, so this matches runtime resolution.
    """
    p = Path(jobs_file).resolve()
    parts = p.parts
    # Find the 'profiles' anchor, then take the next component as the profile name.
    for i, part in enumerate(parts):
        if part == "profiles" and i + 1 < len(parts):
            return parts[i + 1]
    return None


def run_self_test() -> int:
    """Regression guard: test known jobs against runtime-faithful resolution."""
    jobs_file = str(PROFILES_DIR / "jarvis" / "cron" / "jobs.json")

    # Test cases: (job_id, job_name, profile, expected_pins)
    # Pins must reference LIVE jarvis cron job IDs that exist in the store and
    # whose skill pins resolve via build_skill_index (replaced 2026-08-29: the
    # prior cases 88c545dffd60 / 8b84c382cfa8 no longer exist in the store).
    test_cases = [
        ("3eddc6709f44", "dgx-self-improvement-loop", "jarvis", ["agent-reflection", "error-learner"]),
        ("495e3eb9252b", "portfolio-sizing-adjuster", "jarvis", ["trading-data-analysis"]),
        ("e51c9e2fa5df", "elon-governance-loop", "jarvis",
         ["fleet-governor", "obsidian-knowledge-management"]),
    ]

    all_ok = True
    for job_id, job_name, profile, expected_pins in test_cases:
        index = build_skill_index(profile)
        results = resolve_job_pins(job_id, jobs_file, index)
        resolved = [r for r in results if r["status"] == "OK"]
        missing = [r for r in results if r["status"] == "MISSING"]

        print(f"[{job_name}] ({job_id}, profile={profile}):")
        for r in results:
            marker = "OK" if r["status"] == "OK" else "MISSING"
            path_str = f" -> {r['path']}" if r["path"] else ""
            print(f"  [{marker:>6}] {r['name']}{path_str}")

        if missing:
            print(f"  ** WARNING: {len(missing)} pin(s) MISSING **")
            all_ok = False
        print()

    if all_ok:
        print(f"SELF-TEST PASS: ALL pins resolve (exit 0)")
    else:
        print(f"SELF-TEST FAIL: one or more pins MISSING (exit 1)")

    return 0 if all_ok else 1


def main():
    parser = argparse.ArgumentParser(
        description="Resolve cron skill pins AND job scripts exactly as the Hermes runtime does."
    )
    parser.add_argument("--job-id", help="Cron job ID to resolve pins for")
    parser.add_argument("--jobs-file", help="Path to the profile's cron/jobs.json")
    parser.add_argument("--profile", help="Profile name for pin resolution")
    parser.add_argument("--pins", nargs="*", help="Skill pin names to resolve")
    parser.add_argument("--script-check", action="store_true",
                        help="Resolve the job's 'script' field against the owning "
                             "profile HERMES_HOME/scripts (runtime-faithful; mirrors "
                             "scheduler._run_job_script)")
    parser.add_argument("--self-test", action="store_true", help="Run pin regression guard")
    parser.add_argument("--script-self-test", action="store_true",
                        help="Run script-resolution regression guard (job 7ad0e11f7790 + negative)")
    parser.add_argument("--prompt-self-test", action="store_true",
                        help="Run prompt-regression guard for Beat-4 FIRST-RUN PREFLIGHT wording")
    parser.add_argument("--prompt-self-test-negative-fixture", action="store_true",
                        help="Run negative fixture for prompt-regression guard")
    parser.add_argument("--capability-assert", metavar="FLAG",
                        help="Assert THIS resolver advertises FLAG via --help before a "
                             "caller relies on it. Exit 0 if present; exit 2 (with a "
                             "MECHANISM-GAP routing diagnostic) if missing. Guards the "
                             "governor FIRST-RUN PREFLIGHT against a per-task branch "
                             "rotation silently dropping a required flag.")
    parser.add_argument("--guard-regression-self-test", action="store_true",
                        help="Run regression guard proving the capability-assert catches "
                             "the pin-only --script-check rollback (2026-08-01 incident).")

    args = parser.parse_args()

    if args.guard_regression_self_test:
        sys.exit(run_guard_regression_self_test())

    if args.capability_assert:
        sys.exit(run_capability_assert(args.capability_assert))

    if args.prompt_self_test:
        sys.exit(run_prompt_self_test())

    if args.prompt_self_test_negative_fixture:
        sys.exit(run_prompt_self_test_negative_fixture())

    if args.script_self_test:
        sys.exit(run_script_self_test())

    if args.self_test:
        sys.exit(run_self_test())

    if args.script_check:
        if not (args.job_id and args.jobs_file):
            print("USAGE ERROR: --script-check requires --job-id and --jobs-file",
                  file=sys.stderr)
            sys.exit(2)
        res = resolve_job_script(args.job_id, args.jobs_file)
        print(json.dumps(res, indent=2, sort_keys=True))
        sys.exit(0 if res.get("status") in ("RESOLVED", "NO-SCRIPT") else 1)

    if args.job_id and args.jobs_file:
        profile = args.profile or infer_profile_from_jobs_file(args.jobs_file)
        if not profile:
            print(f"USAGE ERROR: Cannot infer profile from jobs-file {args.jobs_file}. "
                  f"Pass --profile explicitly.", file=sys.stderr)
            sys.exit(2)
        index = build_skill_index(profile)
        results = resolve_job_pins(args.job_id, args.jobs_file, index)
    elif args.profile and args.pins:
        index = build_skill_index(args.profile)
        results = resolve_pins(args.pins, index)
    else:
        print("USAGE ERROR: Provide --self-test, --script-self-test, --script-check, "
              "or --job-id+--jobs-file, or --profile+--pins",
              file=sys.stderr)
        parser.print_help(file=sys.stderr)
        sys.exit(2)

    missing = [r for r in results if r["status"] == "MISSING"]
    for r in results:
        marker = "OK" if r["status"] == "OK" else "MISSING"
        path_str = f" -> {r['path']}" if r["path"] else ""
        print(f"[{marker:>6}] {r['name']}{path_str}")

    if missing:
        print(f"\nMISSING pins: {', '.join(r['name'] for r in missing)}")
        sys.exit(1)
    else:
        all_count = len(results)
        print(f"\nALL RESOLVED {all_count}/{all_count} (exit 0)")
        sys.exit(0)


if __name__ == "__main__":
    main()
