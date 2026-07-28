#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Elon kanban watchdog — runs every 1m, no LLM. Reports tasks needing attention.

FIXED (t_452ac936 / t_838b8d66):
- Corrected hermes CLI arg order: --board BEFORE subcommand
- Increased timeout to 60s (was 15s) to survive fleet I/O/gateway latency
- Distinguishes empty-result from command-error/timeout:
  * error/timeout -> STATE-SIGNAL-DEGRADED (NOT ELON IDLE)
  * empty valid result -> ELON IDLE (genuine idle, not blind spot)
"""
import json
import subprocess
import sys
import os


def main() -> int:
    try:
        result = subprocess.run(
            # --board is global to `hermes kanban` and MUST precede the subcommand
            ["hermes", "kanban", "--board", "jarvis-os", "list", "--assignee", "elon", "--json"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": os.path.expanduser("~")}
        )

        # Error path: distinguish command-error from empty-result
        if result.returncode != 0 or result.stderr.strip():
            # Command failed or produced stderr — do NOT resolve to ELON IDLE
            detail = result.stderr.strip() if result.stderr.strip() else f"exit code {result.returncode}"
            print(f"STATE-SIGNAL-DEGRADED: hermes CLI error — {detail}")
            return 0  # exit 0: downstream never runs on a failed-signal context

        stdout = result.stdout or ""
        if not stdout.strip():
            print("ELON IDLE: no tasks, self-improvement window open")
            return 0

        tasks = json.loads(stdout)
        todo_or_ready = [t for t in tasks if t.get("status") in ("todo", "ready")]
        running = [t for t in tasks if t.get("status") == "running"]
        blocked = [t for t in tasks if t.get("status") == "blocked"]

        if running:
            print(f"ELON BUSY: {len(running)} task(s) running — skip")
            return 0

        if todo_or_ready:
            for t in todo_or_ready[:3]:
                print(f"READY: {t['id']} | {t['title'][:80]} | priority={t.get('priority', 0)}")

        if blocked:
            print(f"BLOCKED: {len(blocked)} task(s)")

        if not todo_or_ready and not running:
            print("ELON IDLE: no tasks, self-improvement window open")

        return 0

    except subprocess.TimeoutExpired:
        # Transient fleet I/O/gateway latency — do NOT resolve to ELON IDLE
        print("STATE-SIGNAL-DEGRADED: hermes CLI timed out after 60s (transient fleet latency)")
        return 0
    except json.JSONDecodeError as e:
        print(f"STATE-SIGNAL-DEGRADED: failed to parse kanban JSON — {e}")
        return 0
    except Exception as e:
        print(f"STATE-SIGNAL-DEGRADED: unexpected watchdog error — {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
