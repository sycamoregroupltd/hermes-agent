#!/usr/bin/env bash
# tick-5m — guard/watchdog bundle (5-minute cadence). CONDENSE 1/4 t_db689c47.
# Absorbed checks keep their original cadence via interval gating in the runner.
set -uo pipefail
exec /usr/bin/env python3 /home/frank/.hermes/profiles/jarvis/scripts/cron_guard_bundle_runner.py 5m
