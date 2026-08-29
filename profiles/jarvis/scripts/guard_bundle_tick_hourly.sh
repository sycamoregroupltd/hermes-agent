#!/usr/bin/env bash
# tick-hourly — guard/watchdog bundle (hourly cadence). CONDENSE 1/4 t_db689c47.
set -uo pipefail
exec /usr/bin/env python3 /home/frank/.hermes/profiles/jarvis/scripts/cron_guard_bundle_runner.py hourly
