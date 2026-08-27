#!/usr/bin/env python3
# Auto-generated shim: routes 'blocked-task-notifier' output to the BOARD instead of discord:#critical-alerts.
# Canonical logic untouched at /home/frank/.hermes/scripts/blocked-task-notifier.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/blocked-task-notifier.sh")
os.environ.setdefault("RTB_KEY", "blocked-task-notifier")
os.environ.setdefault("RTB_TITLE", "blocked-task-notifier")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
