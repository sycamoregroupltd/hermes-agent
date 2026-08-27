#!/usr/bin/env python3
# Auto-generated shim: routes 'fusion-calibration-report' output to the BOARD instead of discord:#quant-reports.
# Canonical logic untouched at /home/frank/.hermes/scripts/fusion_calibration_report.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/fusion_calibration_report.sh")
os.environ.setdefault("RTB_KEY", "fusion-calibration-report")
os.environ.setdefault("RTB_TITLE", "fusion-calibration-report")
os.environ.setdefault("RTB_BOARD", "sycode-trading")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
