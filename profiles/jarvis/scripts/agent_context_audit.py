#!/usr/bin/env python3
"""No-agent watchdog: audit Hermes fleet profiles for required-invariant file
presence, project-local guidance contract, and profile-local skill collisions.

Writes:
  /home/frank/uaa-rules/AGENT-CONTEXT-AUDIT.md  (human-readable report)
  /home/frank/uaa-rules/agent-context-audit.json (machine-readable artifact)

Stdout output only when material drift exists (non-empty findings).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERMES_ROOT = Path("/home/frank/.hermes")
PROFILES_DIR = HERMES_ROOT / "profiles"
UAARULES = Path("/home/frank/uaa-rules")
SKILLS_DIR = HERMES_ROOT / "skills"
AGENTS_MD_FILES = ("AGENTS.md", "CLAUDE.md")


def find_profiles() -> list[Path]:
    """Return every non-hidden subdirectory under profiles/."""
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        p for p in PROFILES_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def check_invariants(profile_path: Path) -> list[str]:
    """Check a single profile's invariant files. Return list of issue strings."""
    issues: list[str] = []
    name = profile_path.name

    soul_md = profile_path / "SOUL.md"
    if not soul_md.exists():
        issues.append(f"{name}: MISSING SOUL.md")

    reflection_md = profile_path / "REFLECTION.md"
    if not reflection_md.exists():
        issues.append(f"{name}: MISSING REFLECTION.md")

    # Check that no SKILL_PRUNED markers are present (detects compression loss)
    for fname in ("SOUL.md", "profile.yaml"):
        fpath = profile_path / fname
        if fpath.exists():
            try:
                content = fpath.read_text(errors="replace")
                pruned_count = content.count("[SKILL_PRUNED]")
                if pruned_count > 0:
                    issues.append(
                        f"{name}/{fname}: {pruned_count} [SKILL_PRUNED] marker(s) — skill content was lost to compression"
                    )
            except Exception:
                pass

    # Check skills dir for name collisions
    for skill_name_file in SKILLS_DIR.rglob("SKILL.md"):
        # This skill may be listed in multiple profiles; skip if owner matches
        relative = skill_name_file.relative_to(SKILLS_DIR)
        parts = str(relative).split(os.sep)
        if len(parts) >= 2 and parts[0] != name:
            continue
        if not skill_name_file.parent.samefile(profile_path):
            pass  # cross-profile skill refs are fine

    # Check project-local guidance invariants via AGENTS.md/CLAUDE.md scan
    for ag_file in AGENTS_MD_FILES:
        candidate = profile_path / ag_file
        if candidate.exists():
            try:
                text = candidate.read_text(errors="replace")
                if "obsidian-knowledge-management" not in text:
                    issues.append(
                        f"{name}/{ag_file}: does NOT mention obsidian-knowledge-management skill"
                    )
            except Exception:
                pass

    return issues


def detect_skill_collisions() -> list[str]:
    """Detect skill names registered in more than one profile directory."""
    profile_skills: dict[str, set[str]] = {}
    if not SKILLS_DIR.is_dir():
        return []

    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        if skill_dir == profile_paths_root().resolve():
            # Could be a profile-specific skills folder nested inside profiles/
            pass
        for soul in skill_dir.rglob("SKILL.md"):
            try:
                frontmatter = soul.read_text(errors="replace").split("---", 2)[-1].split("---", 1)[0] if "---" in soul.read_text(errors="replace") else ""
                name_line = ""
                for line in frontmatter.splitlines():
                    if line.strip().startswith("name:") or line.strip().startswith("- name:"):
                        name_line = line.strip()
                        break
                if not name_line:
                    continue
                skill_name = name_line.split(":")[1].strip().strip("'\"")
                if skill_name:
                    profile_skills.setdefault(skill_name, set()).add(skill_dir.name)
            except Exception:
                continue

    collisions: list[str] = []
    for skill_name, owners in profile_skills.items():
        if len(owners) > 1:
            collisions.append(
                f"skill '{skill_name}' present in {sorted(owners)}"
            )
    return collisions


def profile_paths_root() -> Path:
    return PROFILES_DIR


def run_audit() -> dict:
    UAARULES.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()

    profiles = find_profiles()
    all_issues: list[str] = []
    per_profile: dict[str, list[str]] = {}

    for pp in profiles:
        issues = check_invariants(pp)
        per_profile[pp.name] = issues
        all_issues.extend(issues)

    collisions = detect_skill_collisions()
    all_issues.extend(collisions)

    md_lines: list[str] = [
        "# Agent Context Audit",
        "",
        f"**Generated:** {timestamp}",
        "",
        f"**Profiles scanned:** {len(profiles)}",
        f"**Total issues found:** {len(all_issues)}",
        "",
    ]

    if collisions:
        md_lines.append("## Skill Collisions")
        md_lines.append("")
        for c in collisions:
            md_lines.append(f"- {c}")
        md_lines.append("")

    for pname, pissues in sorted(per_profile.items()):
        status = "OK" if not pissues else f"**{len(pissues)} issue(s)**"
        md_lines.append(f"### {pname} — {status}")
        md_lines.append("")
        for iss in pissues:
            md_lines.append(f"- {iss}")
        md_lines.append("")

    result = {
        "timestamp": timestamp,
        "profiles_scanned": len(profiles),
        "total_issues": len(all_issues),
        "collisions": collisions,
        "per_profile": per_profile,
    }

    uaarules_json = UAARULES / "agent-context-audit.json"
    uaarules_md = UAARULES / "AGENT-CONTEXT-AUDIT.md"

    uaarules_json.write_text(json.dumps(result, indent=2, sort_keys=True))
    uaarules_md.write_text("\n".join(md_lines) + "\n")

    if all_issues:
        print("DRIFT DETECTED:", json.dumps({"issues": all_issues}, indent=2))
    else:
        print("CLEAN")

    return result


if __name__ == "__main__":
    try:
        run_audit()
        sys.exit(0)
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
