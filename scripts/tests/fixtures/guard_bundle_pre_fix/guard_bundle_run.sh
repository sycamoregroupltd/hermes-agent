#!/usr/bin/env bash
# Generic no-arg runner for the guard/watchdog bundle, so the bundle can be routed
# through report-to-board.py.
#
# WHY THIS EXISTS: report-to-board.py invokes RTB_SCRIPT with NO arguments
# (`runner = ["bash", script]`), but cron_guard_bundle_runner.py needs a cadence
# argument. Environment IS inherited by that subprocess, so the cadence arrives as
# GUARD_TICK instead of argv. One file serves all four ticks.
#
# The bundle condenses 38 guards/watchdogs/probes into 4 cron jobs (CONDENSE 1/4,
# t_db689c47). Before 2026-08-29 a failing check surfaced ONLY as a red cron: no
# card, no consumer, nothing in the pipe Frank actually reads. That is the black
# hole this routing closes — the same reason report-to-board.py was written
# ("all reports need to go back through the pipe", Frank 2026-08-27).
set -uo pipefail

TICK="${GUARD_TICK:?GUARD_TICK must be set (5m|15m|hourly|daily)}"
RUNNER=/home/frank/.hermes/profiles/jarvis/scripts/cron_guard_bundle_runner.py

if [[ ! -f "$RUNNER" ]]; then
  echo "GUARD BUNDLE ERROR: missing runner $RUNNER" >&2
  exit 1
fi

exec /usr/bin/env python3 "$RUNNER" "$TICK"
