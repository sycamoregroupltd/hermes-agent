#!/usr/bin/env bash
# tick-15m — guard/watchdog bundle (15m cadence). CONDENSE 1/4 t_db689c47.
#
# Routed through report-to-board.py since 2026-08-29: a failing check now files a
# BOARD CARD as well as reddening the cron. Previously the bundle condensed 38
# guards into 4 jobs whose failures had no consumer at all — red cron, no card,
# nothing in the pipe. report-to-board gives one self-closing card per job
# (key rtb-guard-bundle-15m), silent when clean, exit code preserved.
set -uo pipefail
# Pin the profile root: kanban workers and manual probes may export another
# HERMES_HOME, which would make the shared runner resolve scripts/state there.
# GUARD_BUNDLE_ROOT is an explicit fixture relocation seam; production defaults
# remain the live /home/frank/.hermes tree.
GUARD_BUNDLE_ROOT="${GUARD_BUNDLE_ROOT:-/home/frank/.hermes}"
export GUARD_BUNDLE_ROOT
export HERMES_HOME="${GUARD_BUNDLE_ROOT}/profiles/jarvis"
export GUARD_TICK=15m
export RTB_OBSERVATION_PROTOCOL=guard-bundle-v1
# t_8cdc9260 (2026-08-31): runner's own wall-clock budget for this cadence is
# 240s (see BUDGETS in cron_guard_bundle_runner.py) + startup slack. Stays
# well under the scheduler job cap (3600s default).
export RTB_TIMEOUT=300
export RTB_SCRIPT="${GUARD_BUNDLE_ROOT}/scripts/guard_bundle_run.sh"
export RTB_KEY=guard-bundle-15m
export RTB_TITLE="Guard bundle (15m): a fleet guard/watchdog check is failing"
export RTB_BOARD=jarvis-os
exec /usr/bin/env python3 "${GUARD_BUNDLE_ROOT}/scripts/report-to-board.py"
