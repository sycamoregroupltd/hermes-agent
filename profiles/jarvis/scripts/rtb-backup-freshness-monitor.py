#!/usr/bin/env python3
# Auto-generated shim: routes 'backup-freshness-monitor' output to the BOARD instead of telegram.
# Canonical logic untouched at /home/frank/.hermes/scripts/backup-freshness-monitor.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/backup-freshness-monitor.sh")
os.environ.setdefault("RTB_KEY", "backup-freshness-monitor")
os.environ.setdefault("RTB_TITLE", "backup-freshness-monitor")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
