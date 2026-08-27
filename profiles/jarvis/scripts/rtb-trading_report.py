#!/usr/bin/env python3
# Auto-generated shim: routes 'trading_report' output to the BOARD instead of discord:#quant-reports.
# Canonical logic untouched at /home/frank/.hermes/scripts/daily_trading_report.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/daily_trading_report.sh")
os.environ.setdefault("RTB_KEY", "trading_report")
os.environ.setdefault("RTB_TITLE", "trading_report")
os.environ.setdefault("RTB_BOARD", "sycode-trading")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
