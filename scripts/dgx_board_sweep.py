#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
import subprocess, datetime, os
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
date = now[:10]

# CEO HUNT/ADD/GO producer guard (t_e1eeaf67, 2026-08-28): tripwire for empty-body /
# missing-completion-contract CEO cards created recently. Read-only; non-fatal to the sweep.
_guard = subprocess.run(
    ["python3", "/home/frank/.hermes/scripts/ceo_hunt_card_contract_guard.py",
     "--hours", "24", "--board", "sycode-trading"],
    capture_output=True, text=True, timeout=120,
)
with open(f"/tmp/dgx_board_sweep_{date}.txt", "a") as g:
    g.write(_guard.stdout)
    if _guard.returncode != 0:
        g.write(f"[guard] CEO card producer guard found violations (rc={_guard.returncode})\n")
        g.write(_guard.stderr)

subprocess.run([
    "hermes", "-p", "jarvis", "-m", "grok-4.3", "--provider", "xai-oauth", "-z",
    f"Board sweep. Check jarvis-os, upero, sycode-ai, sycode-trading, and yorkstone-supplies kanban boards for blocked/ready/running tasks older than 1 hour. Update STATUS.md artifacts at ~/jarvis/workspace/goals/upero/STATUS.md, ~/jarvis/workspace/goals/sycode-ai/STATUS.md, ~/jarvis/workspace/goals/sycode-trading/STATUS.md, and ~/jarvis/workspace/goals/yorkstone-supplies/STATUS.md when present. Write summary to /tmp/dgx_board_sweep_{date}.txt. No autonomous runtime actions. NOTE: if the CEO card producer guard above reported NEW (post-fix) violations, surface exactly one card to jarvis-os-pm with the violating task ids and evidence; historical backlog lines are informational and must NOT be routed."
], timeout=900)
