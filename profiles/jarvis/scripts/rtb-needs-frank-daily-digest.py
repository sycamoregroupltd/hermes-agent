#!/usr/bin/env python3
# Auto-generated shim: routes 'needs-frank-daily-digest' output to the BOARD instead of discord:#critical-alerts.
# Canonical logic untouched at /home/frank/.hermes/scripts/needs_frank_daily_digest.py; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/needs_frank_daily_digest.py")
os.environ.setdefault("RTB_KEY", "needs-frank-daily-digest")
os.environ.setdefault("RTB_TITLE", "needs-frank-daily-digest")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
