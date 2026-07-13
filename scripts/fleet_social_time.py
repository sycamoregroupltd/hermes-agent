#!/usr/bin/env python3
"""Generate Hermes Agent Social Time discussion digest.

This is designed to be delivered to a Telegram group once the group target is
visible to Hermes. Until then it writes a local digest.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

REFLECTION_REPORT = Path("/home/frank/uaa-rules/FLEET-REFLECTION-REPORT.md")
PROGRAM = Path("/home/frank/uaa-rules/AGENT-SOCIAL-TIME.md")
OUT = Path("/home/frank/uaa-rules/FLEET-SOCIAL-TIME-LATEST.md")
INVITE = "https://t.me/+euOUwCoCSwg0MDZk"
NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

report = (
    REFLECTION_REPORT.read_text(errors="replace") if REFLECTION_REPORT.exists() else ""
)
profiles = []
for m in re.finditer(r"^###\s+(.+)$", report, re.M):
    name = m.group(1).strip()
    if name and len(profiles) < 12:
        profiles.append(name)

# Pick representative voices rather than every clone profile.
preferred = [
    "jarvis",
    "elon",
    "guardian",
    "jarvis-os-pm",
    "sycode-trading-pm",
    "system-optimizer",
    "nervous-system-engineer",
    "research",
    "trading-strategy-dev",
]
participants = [p for p in preferred if f"### {p}" in report]
if len(participants) < 5:
    participants += [p for p in profiles if p not in participants]
participants = participants[:8]

lines = []
lines.append("Agent Social Time")
lines.append(f"Generated: {NOW}")
lines.append(f"Intended Telegram channel/group: {INVITE}")
lines.append("")
lines.append(
    "Theme: becoming better agents through reflection, shared research, and concrete fleet improvements."
)
lines.append("")
lines.append("Participants:")
for p in participants:
    lines.append(
        f"- {p}: review your REFLECTION.md, share one useful learning, and propose one concrete improvement that helps the team."
    )
lines.append("")
lines.append("Discussion prompts:")
lines.append(
    "- What did your latest reflection reveal about your purpose and highest-leverage improvement?"
)
lines.append(
    "- Which project-local AGENTS.md, CLAUDE.md, or .claude skill/plugin should the fleet adopt or port into Hermes?"
)
lines.append(
    "- Which repeated blocker/noisy process should become a skill, script, monitor, or kanban card?"
)
lines.append(
    "- What research learning changes how we should build, verify, or coordinate?"
)
lines.append("")
lines.append(
    "Current reflection report: /home/frank/uaa-rules/FLEET-REFLECTION-REPORT.md"
)
lines.append("Social Time program: /home/frank/uaa-rules/AGENT-SOCIAL-TIME.md")
lines.append("")
lines.append("Routed actions:")
lines.append(
    "- Pending Telegram binding: Hermes currently cannot see the group target. Add the Hermes Telegram bot/account to the group or provide the numeric chat ID, then set this cron delivery target to that Telegram chat."
)

OUT.write_text("\n".join(lines).rstrip() + "\n")
print("\n".join(lines))
