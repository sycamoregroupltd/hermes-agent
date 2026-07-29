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

Exit codes:
    0   All pins resolved
    1   One or more pins missing
    2   Usage error
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
    """Infer owning profile from jobs-file path pattern: profiles/<name>/cron/jobs.json"""
    p = Path(jobs_file).resolve()
    try:
        rel = p.relative_to(PROFILES_DIR)
        parts = rel.parts
        if len(parts) >= 1:
            return parts[0]
    except ValueError:
        pass
    return None


def run_self_test() -> int:
    """Regression guard: test known jobs against runtime-faithful resolution."""
    jobs_file = str(PROFILES_DIR / "jarvis" / "cron" / "jobs.json")

    # Test cases: (job_id, job_name, profile, expected_pins)
    test_cases = [
        ("88c545dffd60", "jarvis-daily-mechanism-liveness", "jarvis", ["gap-plugging"]),
        ("495e3eb9252b", "portfolio-sizing-adjuster", "jarvis", ["trading-data-analysis"]),
        ("8b84c382cfa8", "confluence-edge-research", "jarvis",
         ["quant-research-operations", "external-strategy-discovery",
          "multivariate-pattern-mining", "grok-cli", "obsidian-knowledge-management"]),
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
        description="Resolve cron skill pins exactly as the Hermes runtime does."
    )
    parser.add_argument("--job-id", help="Cron job ID to resolve pins for")
    parser.add_argument("--jobs-file", help="Path to the profile's cron/jobs.json")
    parser.add_argument("--profile", help="Profile name for pin resolution")
    parser.add_argument("--pins", nargs="*", help="Skill pin names to resolve")
    parser.add_argument("--self-test", action="store_true", help="Run regression guard")

    args = parser.parse_args()

    if args.self_test:
        sys.exit(run_self_test())

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
        print("USAGE ERROR: Provide --self-test, or --job-id+--jobs-file, or --profile+--pins",
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
