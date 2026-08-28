#!/usr/bin/env bash
# Profile-local exec shim — hermes cron resolves script names profile-locally first.
export RTB_SCRIPT=/home/frank/.hermes/scripts/verdict-blackhole-report.sh
export RTB_KEY=verdict-blackhole
export RTB_TITLE="Review black holes: out-of-contract REVIEW_VERDICTs never routed"
export RTB_BOARD=jarvis-os
exec /usr/bin/env python3 /home/frank/.hermes/scripts/report-to-board.py
