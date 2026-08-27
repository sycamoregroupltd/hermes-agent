#!/usr/bin/env python3
# Auto-generated shim: routes 'model-deal-scanner' output to the BOARD instead of telegram.
# Canonical logic untouched at /home/frank/.hermes/scripts/model-deal-scanner.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/model-deal-scanner.sh")
os.environ.setdefault("RTB_KEY", "model-deal-scanner")
os.environ.setdefault("RTB_TITLE", "model-deal-scanner")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
