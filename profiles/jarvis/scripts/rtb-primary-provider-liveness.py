#!/usr/bin/env python3
# Auto-generated shim: routes 'primary-provider-liveness' output to the BOARD instead of discord:#critical-alerts,telegram.
# Canonical logic untouched at /home/frank/.hermes/scripts/nous_token_presence.sh; delivery changed only.
import os, sys
os.environ.setdefault("RTB_SCRIPT", "/home/frank/.hermes/scripts/nous_token_presence.sh")
os.environ.setdefault("RTB_KEY", "primary-provider-liveness")
os.environ.setdefault("RTB_TITLE", "primary-provider-liveness")
os.environ.setdefault("RTB_BOARD", "jarvis-os")
os.execv(sys.executable, [sys.executable,
    "/home/frank/.hermes/scripts/report-to-board.py", *sys.argv[1:]])
