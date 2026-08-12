#!/usr/bin/env python3
"""Fleet guard: durable Hermes config must never reference an ephemeral kanban workspace.

Kanban workspaces (~/.hermes/kanban/boards/<board>/workspaces/t_*) are garbage-collected.
Anything wired into one (MCP PYTHONPATH, cron script/workdir, command path) becomes
permanently unstartable and retries in a tight loop, burning log volume and wall-clock.

Origin: kanban t_b39d4001 (2026-08-02) — builder's git/filesystem/postgres MCP servers
pointed at workspaces/t_47931a99/src, which no longer existed. The same scan immediately
found a live-but-broken cron in trading-data-oracle (filed as t_fbc62972).

HOME: this file is the canonical live copy, re-homed from the devops cron store (which has
no gateway and never ticks) into the jarvis store by t_ea85c132. Keep it a REAL FILE under
<HERMES_HOME>/scripts/ — the scheduler resolves bare script names there, and a symlink or
absolute path is not honoured.

Scope of what is reported:
  - ANY reference in a profile/root config.yaml (always durable, always actionable)
  - ENABLED cron jobs only (a disabled job pointing at a dead workspace is inert noise)

no_agent cron contract: exits 0 and prints NOTHING when clean (silent == healthy);
prints the findings and still exits 0 when drift is found, so the scheduler treats it as
a deliverable message rather than a job failure.

Runbook: /home/frank/obsidian-fleet-vault/Runbooks/mcp-config-durability-and-git-mcp-pin.md
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def _hermes_root() -> Path:
    """Resolve the fleet root independently of a possibly-scoped HOME.

    Cron sessions can run with HOME scoped to a profile sandbox. If that happened
    and we silently resolved to a nonexistent root, the guard would scan zero files
    and print nothing — and under the no_agent contract "prints nothing" means
    HEALTHY. That would turn this guard into exactly the false-confidence artifact
    it exists to prevent, so the scan root is derived from HERMES_HOME when present
    and sanity-checked in main().
    """
    explicit = os.environ.get("HERMES_ROOT")
    if explicit:
        return Path(explicit)
    # HERMES_HOME is <root>/profiles/<name> for profile-scoped runs (what the
    # gateway ticker sets); its grandparent is the fleet root.
    home = os.environ.get("HERMES_HOME")
    if home:
        p = Path(home)
        if p.parent.name == "profiles":
            return p.parent.parent
        return p
    return Path.home() / ".hermes"


HERMES = _hermes_root()
PROFILES = HERMES / "profiles"
WORKSPACE_RE = re.compile(r"kanban/boards/[^/\s\"']+/workspaces/(t_[0-9a-f]+)")
RUNBOOK = "/home/frank/obsidian-fleet-vault/Runbooks/mcp-config-durability-and-git-mcp-pin.md"


def _ws_state(rel: str) -> str:
    return "GONE" if not (HERMES / rel).exists() else "exists-but-ephemeral"


def scan_configs() -> list[str]:
    """Any workspace reference in a config.yaml is durable config and always reportable."""
    out: list[str] = []
    targets = sorted(PROFILES.glob("*/config.yaml"))
    root_cfg = HERMES / "config.yaml"
    if root_cfg.exists():
        targets.append(root_cfg)
    for path in targets:
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            out.append(f"UNREADABLE {path}: {exc}")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            m = WORKSPACE_RE.search(line)
            if m:
                out.append(f"{path}:{lineno}: workspace {m.group(1)} ({_ws_state(m.group(0))})")
    return out


def scan_crons() -> list[str]:
    """Only ENABLED cron jobs — a disabled job pointing at a dead workspace is inert."""
    out: list[str] = []
    for path in sorted(PROFILES.glob("*/cron/jobs.json")):
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            out.append(f"UNREADABLE {path}: {exc}")
            continue
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict) or not job.get("enabled", False):
                continue
            for field in ("workdir", "script", "prompt"):
                val = job.get(field) or ""
                if not isinstance(val, str):
                    continue
                m = WORKSPACE_RE.search(val)
                if not m:
                    continue
                out.append(
                    f"{path}: ENABLED job {job.get('id')} ({job.get('name')}) "
                    f"{field} -> workspace {m.group(1)} ({_ws_state(m.group(0))})"
                )
                break
    return out


def main() -> int:
    # Silence is only meaningful if we actually scanned the fleet. A scan root with
    # no profiles means the environment is wrong, not that the fleet is clean —
    # report loudly instead of exiting quiet and healthy-looking.
    profile_configs = list(PROFILES.glob("*/config.yaml")) if PROFILES.is_dir() else []
    if not profile_configs:
        print("EPHEMERAL-WORKSPACE GUARD CANNOT SCAN — silence would be misleading")
        print(f"  resolved scan root: {HERMES}")
        print(f"  profiles dir: {PROFILES} (exists={PROFILES.is_dir()})")
        print("  Found 0 profile config.yaml files. Check HERMES_ROOT/HERMES_HOME for this job.")
        return 0

    findings = scan_configs() + scan_crons()
    if not findings:
        return 0
    print("EPHEMERAL-WORKSPACE REFERENCE IN DURABLE HERMES CONFIG")
    print("Kanban workspaces are garbage-collected; config pointing at one can never start.")
    print(f"Runbook: {RUNBOOK}")
    print(f"Scanned {len(profile_configs)} profile configs under {HERMES}")
    print()
    for f in findings:
        print(f"  {f}")
    # Exit 0 deliberately: under no_agent cron, a non-zero exit is reported as a job
    # failure, whereas stdout is delivered as the alert message. Findings are the
    # message, not a crash.
    return 0


if __name__ == "__main__":
    sys.exit(main())
