#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
import subprocess, datetime, os
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
date = now[:10]
subprocess.run([
    "hermes", "-p", "jarvis", "-m", "grok-4.3", "--provider", "xai-oauth", "-z",
    f"Board sweep. Check jarvis-os, upero, sycode-ai, sycode-trading, and yorkstone-supplies kanban boards for blocked/ready/running tasks older than 1 hour. Update STATUS.md artifacts at ~/jarvis/workspace/goals/upero/STATUS.md, ~/jarvis/workspace/goals/sycode-ai/STATUS.md, ~/jarvis/workspace/goals/sycode-trading/STATUS.md, and ~/jarvis/workspace/goals/yorkstone-supplies/STATUS.md when present. Write summary to /tmp/dgx_board_sweep_{date}.txt. No autonomous runtime actions."
], timeout=900)
