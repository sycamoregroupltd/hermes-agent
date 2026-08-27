#!/usr/bin/env python3
# Auto-generated shim: routes 'weekly-insights-digest' output to the BOARD instead of discord:#fleet-reports.
# Canonical logic untouched at /home/frank/.hermes/profiles/jarvis/scripts/weekly_insights_digest.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/profiles/jarvis/scripts/weekly_insights_digest.sh")
os.environ.setdefault("RTB_KEY", "weekly-insights-digest")
os.environ.setdefault("RTB_TITLE", "weekly-insights-digest")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
