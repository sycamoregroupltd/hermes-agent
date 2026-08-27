#!/usr/bin/env python3
# Auto-generated shim: routes 'sweet_spot_calibration' output to the BOARD instead of discord:#quant-reports.
# Canonical logic untouched at /home/frank/.hermes/scripts/sweet_spot_calibration.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/sweet_spot_calibration.sh")
os.environ.setdefault("RTB_KEY", "sweet_spot_calibration")
os.environ.setdefault("RTB_TITLE", "sweet_spot_calibration")
os.environ.setdefault("RTB_BOARD", "sycode-trading")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
