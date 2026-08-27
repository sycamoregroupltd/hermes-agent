#!/usr/bin/env python3
# Auto-generated shim: routes 'dgx-unified-health-probe' output to the BOARD instead of discord:#fleet-reports.
# Canonical logic untouched at /home/frank/.hermes/scripts/dgx_unified_health_probe.py; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/dgx_unified_health_probe.py")
os.environ.setdefault("RTB_KEY", "dgx-unified-health-probe")
os.environ.setdefault("RTB_TITLE", "dgx-unified-health-probe")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
