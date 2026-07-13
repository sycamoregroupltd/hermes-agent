#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Guide curator — clone of dgx_skill_curator.py for the Obsidian Research/Guides vault.

Mirrors `hermes curator` semantics for knowledge guides: review Research/Guides/ for
duplicate/superseded guides + stray research-drafts, CONSOLIDATE overlaps, and ARCHIVE
the obsolete to Guides/.archive/ (recoverable — never deleted). Dry-run by default;
pass --live to apply. Backs up Guides/ before any live change. Pin-protects guides with
`pinned: true` frontmatter. Intended to run on a cron (e.g. every 3d), dry-run first.
"""
import subprocess, datetime, os, sys, tarfile

GUIDES = "/home/frank/obsidian-fleet-vault/Research/Guides"
ARCHIVE = os.path.join(GUIDES, ".archive")
BACKUPS = "/home/frank/obsidian-fleet-vault/Research/.guide_curator_backups"

now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
live = "--live" in sys.argv
mode = "LIVE" if live else "DRY-RUN"

if live:
    os.makedirs(BACKUPS, exist_ok=True)
    os.makedirs(ARCHIVE, exist_ok=True)
    with tarfile.open(os.path.join(BACKUPS, f"guides-{now}.tar.gz"), "w:gz") as t:
        t.add(GUIDES, arcname="Guides")

prompt = (
    f"Guide curator pass ({mode}). Review markdown in {GUIDES} (exclude .archive). "
    "Identify duplicate/superseded guides and stray research-draft notes (frontmatter status: research-draft) "
    "whose content is already captured by a published guide (type: research-guide, status: published). "
    "Keep only published guides as canonical; NEVER touch a guide with frontmatter pinned: true. "
    "Before archiving a duplicate, MERGE any unique content into the canonical guide and bump its version+changelog. "
    f"{'Then ARCHIVE' if live else 'REPORT what you WOULD archive'} superseded/duplicate files by MOVING them to "
    f"{ARCHIVE} (recoverable, never delete). Update {GUIDES}/README.md index. "
    f"Log results to /tmp/dgx_guide_curator_{now[:8]}.txt."
)

subprocess.run(
    ["hermes", "-p", "jarvis", "-m", "gpt-5.5", "--provider", "openai-codex", "-z", prompt],
    timeout=900,
)

