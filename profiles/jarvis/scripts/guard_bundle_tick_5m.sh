#!/usr/bin/env bash
# tick-5m — guard/watchdog bundle (5m cadence). CONDENSE 1/4 t_db689c47.
#
# Routed through report-to-board.py since 2026-08-29: a failing check now files a
# BOARD CARD as well as reddening the cron. Previously the bundle condensed 38
# guards into 4 jobs whose failures had no consumer at all — red cron, no card,
# nothing in the pipe. report-to-board gives one self-closing card per job
# (key rtb-guard-bundle-5m), silent when clean, exit code preserved.
set -uo pipefail
export GUARD_TICK=5m
export RTB_SCRIPT=/home/frank/.hermes/scripts/guard_bundle_run.sh
export RTB_KEY=guard-bundle-5m
export RTB_TITLE="Guard bundle (5m): a fleet guard/watchdog check is failing"
export RTB_BOARD=jarvis-os
exec /usr/bin/env python3 /home/frank/.hermes/scripts/report-to-board.py
