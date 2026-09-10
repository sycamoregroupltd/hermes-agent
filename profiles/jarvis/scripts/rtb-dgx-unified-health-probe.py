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
# t_4ed34e09: suppress re-firing a NEW [report] card every ~13-15min tick
# for a BLOCK whose dead_keys/infra/crash cause signature is byte-identical
# to the last one report-to-board.py acted on — even across a human
# archiving the interim card (see report-to-board.py's dedup_fingerprint()
# + tombstone comments for the full mechanism). 3h quiet window: long
# enough to absorb the ~13-15min cadence duplicate storm this card was
# filed over (20+ in a few hours), short enough that a condition genuinely
# still open after 3h re-surfaces on the board rather than going silent
# indefinitely. A CHANGED dead_keys/infra/crash signature always fires
# immediately regardless of this window (fail-visible requirement).
os.environ["RTB_QUIET_WINDOW_SEC"] = "10800"
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
