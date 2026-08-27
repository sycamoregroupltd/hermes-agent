#!/usr/bin/env python3
# Auto-generated shim: routes 'orchestrator-heartbeat-watchdog' output to the BOARD instead of telegram.
# Canonical logic untouched at /home/frank/.hermes/scripts/orchestrator-heartbeat-watchdog.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/orchestrator-heartbeat-watchdog.sh")
os.environ.setdefault("RTB_KEY", "orchestrator-heartbeat-watchdog")
os.environ.setdefault("RTB_TITLE", "orchestrator-heartbeat-watchdog")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
