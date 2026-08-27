#!/usr/bin/env python3
# Auto-generated shim: routes 'dgx-disk-space-watchdog' output to the BOARD instead of discord:#fleet-reports.
# Canonical logic untouched at /home/frank/.hermes/scripts/dgx_disk_space_watchdog.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/dgx_disk_space_watchdog.sh")
os.environ.setdefault("RTB_KEY", "dgx-disk-space-watchdog")
os.environ.setdefault("RTB_TITLE", "dgx-disk-space-watchdog")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
