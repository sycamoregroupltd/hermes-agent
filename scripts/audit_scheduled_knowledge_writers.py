#!/usr/bin/env python3
"""Inventory active Hermes cron jobs that can write second-brain Markdown.

This is a read-only static control. It resolves profile shims to central scripts,
flags direct vault writers that do not use the canonical writer helper, and
separately reports agent prompts that explicitly request Obsidian/wiki writes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


VAULT_MARKERS = (
    "/home/frank/obsidian-fleet-vault",
    "/home/frank/obsidian/quant-team",
    "/home/frank/obsidian/sycode-trading",
    "~/obsidian/",
    "OBSIDIAN_",
)
WRITE_MARKERS = (
    ".write_text(",
    "open(",
    "write_markdown_atomic(",
    "write_json_atomic(",
    "append_markdown_event(",
    "os.replace(",
    "cp ",
    "> \"$DST",
)
EXEC_PATH = re.compile(r"(?:execv|exec|python3?)\s*\(?[\"']?(/home/frank/\.hermes/scripts/[A-Za-z0-9_.-]+)")
QUOTED_CENTRAL = re.compile(r"[\"'](/home/frank/\.hermes/scripts/[A-Za-z0-9_.-]+)[\"']")
QUOTED_SCRIPT = re.compile(r"[\"'](/home/frank/[A-Za-z0-9_./-]+\.(?:py|sh))[\"']")
SCRIPT_DIR_CHILD = re.compile(r"\$SCRIPT_DIR/([A-Za-z0-9_.-]+\.(?:py|sh))")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.incoming-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def active_jobs(profiles: Path):
    seen: set[str] = set()
    for path in sorted(profiles.glob("*/cron/jobs.json")):
        real = str(path.resolve())
        if real in seen:
            continue
        seen.add(real)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else payload
        if not isinstance(jobs, list):
            continue
        profile = path.parents[1].name
        for job in jobs:
            if isinstance(job, dict) and job.get("enabled", True) and job.get("state") not in {"paused", "disabled", "archived"}:
                yield profile, job


def resolve_script(profiles: Path, shared: Path, profile: str, value: str) -> tuple[Path | None, list[str]]:
    if not value:
        return None, []
    raw = Path(value).expanduser()
    if raw.is_absolute():
        try:
            candidates = [profiles / raw.relative_to("/home/frank/.hermes/profiles")]
        except ValueError:
            try:
                candidates = [shared / raw.relative_to("/home/frank/.hermes/scripts")]
            except ValueError:
                candidates = [raw]
    else:
        candidates = [profiles / profile / "scripts" / raw, shared / raw]
    current = next((path for path in candidates if path.is_file()), None)
    chain = []
    seen = set()
    while current and current not in seen:
        seen.add(current)
        chain.append(str(current))
        text = current.read_text(encoding="utf-8", errors="replace")
        # A resolved producer may itself invoke a report/source script. Once it
        # demonstrably owns a vault write, audit that producer instead of
        # walking past it into its read-only input dependency.
        if any(marker in text for marker in VAULT_MARKERS) and any(marker in text for marker in WRITE_MARKERS):
            break
        matches = [*QUOTED_CENTRAL.findall(text), *EXEC_PATH.findall(text), *QUOTED_SCRIPT.findall(text)]
        targets = {Path(match) for match in matches}
        targets.update(current.parent / name for name in SCRIPT_DIR_CHILD.findall(text))
        unique_targets = sorted(targets)
        target = unique_targets[0] if len(unique_targets) == 1 else None
        if target:
            try:
                remapped = shared / target.relative_to("/home/frank/.hermes/scripts")
                if remapped.is_file():
                    target = remapped
            except ValueError:
                try:
                    remapped = profiles / target.relative_to("/home/frank/.hermes/profiles")
                    if remapped.is_file():
                        target = remapped
                except ValueError:
                    pass
        if target and target.is_file() and target != current:
            current = target
            continue
        break
    return current, chain


def canonical_script_path(path: Path, profiles: Path, shared: Path) -> str:
    resolved = path.resolve()
    try:
        return str(Path("/home/frank/.hermes/scripts") / resolved.relative_to(shared.resolve()))
    except ValueError:
        pass
    try:
        return str(Path("/home/frank/.hermes/profiles") / resolved.relative_to(profiles.resolve()))
    except ValueError:
        return str(resolved)


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="scheduled-writer-audit-test-") as temporary:
        root = Path(temporary)
        profiles = root / "profiles"
        shared = root / "scripts"
        profile_scripts = profiles / "jarvis-os-pm" / "scripts"
        profile_scripts.mkdir(parents=True)
        shared.mkdir()
        (profile_scripts / "pm_reject_monitor.sh").write_text(
            '#!/bin/bash\nSCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'exec python3 "$SCRIPT_DIR/pm_reject_monitor.py" --vault\n',
            encoding="utf-8",
        )
        (profile_scripts / "pm_reject_monitor.py").write_text(
            'SHARED = "/home/frank/.hermes/scripts/pm_reject_monitor.py"\n',
            encoding="utf-8",
        )
        canonical = shared / "pm_reject_monitor.py"
        canonical.write_text(
            'VAULT = "/home/frank/obsidian-fleet-vault"\n'
            'from second_brain_writer import write_markdown_atomic\n'
            'write_markdown_atomic(VAULT, "body")\n',
            encoding="utf-8",
        )
        resolved, chain = resolve_script(profiles, shared, "jarvis-os-pm", "pm_reject_monitor.sh")
        if resolved != canonical or len(chain) != 3:
            raise AssertionError(f"profile wrapper resolution failed: resolved={resolved} chain={chain}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles-dir", type=Path, default=Path("/home/frank/.hermes/profiles"))
    parser.add_argument("--shared-scripts-dir", type=Path, default=Path("/home/frank/.hermes/scripts"))
    parser.add_argument("--exceptions", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print(json.dumps({"status": "pass", "component": "scheduled-writer-audit"}, sort_keys=True))
        return 0
    exception_map = {}
    if args.exceptions:
        value = json.loads(args.exceptions.read_text(encoding="utf-8"))
        exception_map = value.get("exceptions", {}) if isinstance(value, dict) else {}
        if not isinstance(exception_map, dict):
            raise RuntimeError("scheduled-writer exceptions must be a mapping")
    direct: dict[str, dict] = {}
    prompt_writers = []
    unresolved = []
    jobs_scanned = 0
    for profile, job in active_jobs(args.profiles_dir):
        jobs_scanned += 1
        script_value = str(job.get("script") or "")
        prompt = str(job.get("prompt") or "")
        if "obsidian" in prompt.lower() or "wiki" in prompt.lower():
            skills = set()
            if isinstance(job.get("skill"), str) and job["skill"]:
                skills.add(job["skill"])
            if isinstance(job.get("skills"), list):
                skills.update(item for item in job["skills"] if isinstance(item, str) and item)
            prompt_writers.append(
                {
                    "profile": profile,
                    "job_id": job.get("id"),
                    "name": job.get("name"),
                    "no_agent": bool(job.get("no_agent")),
                    "skills": sorted(skills),
                    "script": script_value or None,
                    "workdir": job.get("workdir"),
                }
            )
        if not script_value:
            continue
        script, chain = resolve_script(args.profiles_dir, args.shared_scripts_dir, profile, script_value)
        if script is None:
            unresolved.append({"profile": profile, "job_id": job.get("id"), "script": script_value})
            continue
        text = script.read_text(encoding="utf-8", errors="replace")
        if not any(marker in text for marker in VAULT_MARKERS):
            continue
        if not any(marker in text for marker in WRITE_MARKERS):
            continue
        key = canonical_script_path(script, args.profiles_dir, args.shared_scripts_dir)
        record = direct.setdefault(
            key,
            {
                "script": key,
                "inspected_path": str(script.resolve()),
                "jobs": [],
                "canonical_writer": "second_brain_writer" in text,
                "atomic_replace": "os.replace(" in text or "write_markdown_atomic(" in text or "write_json_atomic(" in text or "append_markdown_event(" in text,
                "profile_chains": [],
            },
        )
        record["jobs"].append({"profile": profile, "job_id": job.get("id"), "name": job.get("name")})
        if chain not in record["profile_chains"]:
            record["profile_chains"].append(chain)
    writers = sorted(direct.values(), key=lambda item: item["script"])
    findings = []
    exceptions_used = []
    for writer in writers:
        exception = exception_map.get(writer["script"])
        if exception:
            if not isinstance(exception, dict) or not exception.get("scope") or not exception.get("reason"):
                findings.append({"kind": "invalid-scheduled-writer-exception", "severity": "high", "script": writer["script"]})
            else:
                exceptions_used.append({"script": writer["script"], **exception})
            continue
        if not writer["canonical_writer"] or not writer["atomic_replace"]:
            findings.append(
                {
                    "kind": "noncanonical-scheduled-knowledge-writer",
                    "severity": "high",
                    "script": writer["script"],
                    "canonical_writer": writer["canonical_writer"],
                    "atomic_replace": writer["atomic_replace"],
                    "jobs": writer["jobs"],
                }
            )
    for writer in prompt_writers:
        if not writer["no_agent"] and "obsidian-knowledge-management" not in writer["skills"]:
            findings.append(
                {
                    "kind": "agent-prompt-knowledge-skill-missing",
                    "severity": "high",
                    "profile": writer["profile"],
                    "job_id": writer["job_id"],
                    "name": writer["name"],
                    "required_skill": "obsidian-knowledge-management",
                    "configured_skills": writer["skills"],
                }
            )
    report = {
        "schema_version": 2,
        "status": "pass" if not findings else "fail",
        "jobs_scanned": jobs_scanned,
        "direct_writers": writers,
        "prompt_writers": prompt_writers,
        "unresolved_scripts": unresolved,
        "exceptions_used": exceptions_used,
        "unmatched_exceptions": sorted(set(exception_map) - {item["script"] for item in writers}),
        "findings": findings,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        atomic_write(args.json_out, rendered)
    print(rendered, end="")
    return 0 if not findings else 2


if __name__ == "__main__":
    sys.exit(main())
