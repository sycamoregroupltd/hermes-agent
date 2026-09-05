#!/usr/bin/env python3
# Auto-generated shim: routes 'nous-balance-watchdog' output to the BOARD instead of discord:#critical-alerts.
# Canonical logic untouched at /home/frank/.hermes/profiles/jarvis/scripts/nous_balance_watchdog.py; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/profiles/jarvis/scripts/nous_balance_watchdog.py")
os.environ.setdefault("RTB_KEY", "nous-balance-watchdog")
os.environ.setdefault("RTB_TITLE", "nous-balance-watchdog")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
# t_06b884a5: falling-edge watchdog — empty stdout during the dedup window
# does NOT mean healthy. Point RTB at the watchdog's own state file (same
# path/env var the script uses) so report-to-board.py can tell "still low,
# just deduped" apart from "genuinely recovered" instead of auto-closing on
# silence alone.
os.environ.setdefault("RTB_STATE_FILE", os.environ.get(
    "NOUS_BALANCE_WATCHDOG_STATE",
    "/home/frank/.hermes/profiles/jarvis/cron/state/nous_balance_watchdog.first_seen.json"))
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
