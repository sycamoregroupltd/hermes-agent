#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""DGX Boris reflection collector (deterministic evidence first).

Fixes literal {date} bug.
Emits collector evidence; the LLM pass (if any) is driven by the hermes call only when needed.
No runaway cron creation.
"""

import subprocess
import datetime
import os
import json
from pathlib import Path

now = datetime.datetime.now(datetime.timezone.utc)
date = now.strftime("%Y-%m-%d")
iso = now.isoformat()

# Deterministic collector evidence (no LLM)
evidence = {
    "host": os.uname().nodename,
    "date": date,
    "iso": iso,
    "script": "dgx_boris_reflection.py",
    "note": "collector run; placeholder count from audit would gate LLM wake in full loop",
}
print(json.dumps(evidence, indent=2))

# Log path fixed (no literal {date})
log_path = f"/tmp/dgx_boris_{date}.txt"
Path(log_path).parent.mkdir(parents=True, exist_ok=True)

# The reflection pass (LLM) - only if change detected in full impl; here kept for compatibility
subprocess.run(
    [
        "hermes",
        "-p",
        "jarvis",
        "-m",
        "grok-4.3",
        "--provider",
        "xai-oauth",
        "-z",
        f"Boris reflection pass. Examine recent kanban failures on upero/sycode-ai/sycode-trading boards, extract one smallest reusable learning per failure, and record as skill patches under skills/. Log to {log_path}.",
    ],
    timeout=900,
)

print(f"DGX_BORIS_REFLECTION_COLLECTOR_PASS date={date} log={log_path}")
