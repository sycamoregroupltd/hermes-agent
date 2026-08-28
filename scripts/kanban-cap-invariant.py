#!/usr/bin/env python3
"""
kanban-cap-invariant.py — Detect when kanban running count exceeds max_in_progress
for an extended period, indicating the board cap is too low for the workload.

Mechanism: invariant-check (no_agent cron script)
Producer: This script, when it detects a breach
Consumer: Discord #critical-alerts (or configured channel), Frank/PM response path
Where-installed: /home/frank/.hermes/scripts/kanban-cap-invariant.py
Schedule: every 15 minutes
A-Tier: A2 (reversible, no mutation, alert-only)
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

BOARDS = [
    "sycode-trading",
    "jarvis-os",
    "upero",
    "yorkstone-supplies",
    "ai-restaurant",
]

# How long running must exceed max_in_progress before alerting (seconds)
BREACH_THRESHOLD_SECONDS = 3600  # 1 hour

# Where to store breach state so we don't alert repeatedly
STATE_FILE = Path("/home/frank/.hermes/state/kanban-cap-invariant.json")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_max_in_progress():
    """Read max_in_progress from hermes config."""
    try:
        import subprocess
        result = subprocess.run(
            ["hermes", "config", "get", "kanban.max_in_progress"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return 8  # default fallback


def check_board(board: str, max_in_progress: int, state: dict):
    """Check if board has been above cap for too long."""
    db_path = Path(f"/home/frank/.hermes/kanban/boards/{board}/kanban.db")
    if not db_path.exists():
        return None

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        running = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='running'"
        ).fetchone()[0]
    finally:
        conn.close()

    now = int(time.time())
    board_state = state.get(board, {})

    if running > max_in_progress:
        # In breach
        breach_start = board_state.get("breach_start")
        if breach_start is None:
            # New breach, record start
            state[board] = {"breach_start": now, "alerted": False}
            return None  # Don't alert yet, wait for threshold

        # Existing breach
        if now - breach_start >= BREACH_THRESHOLD_SECONDS and not board_state.get("alerted"):
            # Threshold exceeded, alert once
            state[board]["alerted"] = True
            return {
                "board": board,
                "running": running,
                "max_in_progress": max_in_progress,
                "breach_duration_minutes": (now - breach_start) // 60,
                "message": (
                    f"🚨 KANBAN CAP BREACH: board={board} "
                    f"running={running} > max_in_progress={max_in_progress} "
                    f"for {(now - breach_start) // 60} minutes. "
                    f"Consider raising kanban.max_in_progress or reducing worker count."
                )
            }
        return None
    else:
        # Not in breach, reset
        if board_state:
            state[board] = {"breach_start": None, "alerted": False}
        return None


def main():
    max_in_progress = get_max_in_progress()
    state = load_state()

    alerts = []
    for board in BOARDS:
        alert = check_board(board, max_in_progress, state)
        if alert:
            alerts.append(alert)

    save_state(state)

    if alerts:
        for alert in alerts:
            print(alert["message"])
        sys.exit(1)  # Non-zero = alert condition for cron
    else:
        # Silent when clean
        sys.exit(0)


if __name__ == "__main__":
    main()
