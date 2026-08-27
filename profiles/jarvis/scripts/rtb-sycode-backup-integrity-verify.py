#!/usr/bin/env python3
# Auto-generated shim: routes 'sycode-backup-integrity-verify' output to the BOARD instead of telegram.
# Canonical logic untouched at /home/frank/.hermes/scripts/verify-sycode-backup-integrity.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/verify-sycode-backup-integrity.sh")
os.environ.setdefault("RTB_KEY", "sycode-backup-integrity-verify")
os.environ.setdefault("RTB_TITLE", "sycode-backup-integrity-verify")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
