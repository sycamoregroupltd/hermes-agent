#!/usr/bin/env python3
# Auto-generated shim: routes 'dgx-unified-health-probe' output to the BOARD instead of discord:#fleet-reports.
# Canonical logic untouched at /home/frank/.hermes/scripts/dgx_unified_health_probe.py; delivery changed only.
import os, sys
# t_6397f2c1: assignment, not setdefault. Under guard-bundle the parent
# already exported RTB_SCRIPT=guard_bundle_run.sh; setdefault was a no-op
# and the probe main() never ran.
os.environ["RTB_SCRIPT"] = "/home/frank/.hermes/scripts/dgx_unified_health_probe.py"
os.environ["RTB_KEY"] = "dgx-unified-health-probe"
os.environ["RTB_TITLE"] = "dgx-unified-health-probe"
os.environ["RTB_BOARD"] = "jarvis-os"
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
