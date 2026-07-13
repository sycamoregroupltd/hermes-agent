#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Elon kanban watchdog — runs every 30s, no LLM. Reports tasks needing attention."""
import json, subprocess, sys, os

try:
    # Check for tasks assigned to elon
    result = subprocess.run(
        ["hermes", "kanban", "list", "--board", "jarvis-os", "--assignee", "elon", "--json"],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "HOME": os.path.expanduser("~")}
    )
    tasks = json.loads(result.stdout) if result.stdout else []
    
    todo_or_ready = [t for t in tasks if t.get("status") in ("todo", "ready")]
    running = [t for t in tasks if t.get("status") == "running"]
    blocked = [t for t in tasks if t.get("status") == "blocked"]
    
    if running:
        print(f"ELON BUSY: {len(running)} task(s) running — skip")
        sys.exit(0)
    
    if todo_or_ready:
        for t in todo_or_ready[:3]:
            print(f"READY: {t['id']} | {t['title'][:80]} | priority={t.get('priority',0)}")
    
    if blocked:
        print(f"BLOCKED: {len(blocked)} task(s)")
    
    if not todo_or_ready and not running:
        print("ELON IDLE: no tasks, self-improvement window open")
    
except Exception as e:
    print(f"WATCHDOG ERROR: {e}")
    sys.exit(1)
