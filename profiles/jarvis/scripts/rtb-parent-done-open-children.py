#!/usr/bin/env python3
"""Route the jarvis-os done-parent/open-child detector to one board report card."""
import os
import sys

os.environ.setdefault("BOARD_WIDE_ONLY", "1")
os.environ.setdefault(
    "RTB_SCRIPT",
    "/home/frank/.hermes/profiles/jarvis/scripts/monitor_convergence_epic.py",
)
os.environ.setdefault("RTB_KEY", "parent-done-open-children")
os.environ.setdefault(
    "RTB_TITLE",
    "jarvis-os done/archived parents still have open children",
)
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.environ.setdefault("RTB_TIMEOUT", "120")
os.execv(
    sys.executable,
    [
        sys.executable,
        "/home/frank/.hermes/scripts/report-to-board.py",
        *sys.argv[1:],
    ],
)
